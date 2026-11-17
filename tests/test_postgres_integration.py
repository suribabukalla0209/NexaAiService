"""Real PostgreSQL integration tests.

Set NEXA_TEST_POSTGRES_URL to a disposable PostgreSQL test database. Tests use
unique user IDs and only clean up their own rows; they never drop shared tables.
"""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete, inspect, text, update
from sqlalchemy.engine import make_url

from app.database.schema import messages, rate_limit_buckets
from app.database.engine import Database
from app.repositories.message_repository import (
    MessageRepository,
    RetryCooldown,
    RetryLimitReached,
)
from app.schemas.messages import ProcessMessageRequest
from app.tools.migrate_sqlite_to_postgres import migrate as migrate_sqlite_to_postgres
from tests.postgres_schema_support import (
    new_schema_name,
    temporary_owned_schema,
    url_for_schema,
)


POSTGRES_URL = os.getenv("NEXA_TEST_POSTGRES_URL", "").strip()
_POSTGRES_URL_VALID = False
if POSTGRES_URL:
    try:
        _test_url = make_url(POSTGRES_URL)
        _test_query = dict(_test_url.query)
        _test_host = (_test_url.host or "").lower()
        _POSTGRES_URL_VALID = (
            _test_url.drivername == "postgresql+psycopg"
            and _test_url.database == "nexa_test"
            and bool(_test_url.username)
            and bool(_test_url.password)
            and _test_host.endswith(".neon.tech")
            and "-pooler." not in _test_host
            and _test_query.get("sslmode", "").lower()
            in {"require", "verify-ca", "verify-full"}
        )
    except Exception:
        _POSTGRES_URL_VALID = False

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not _POSTGRES_URL_VALID,
        reason=(
            "Set NEXA_TEST_POSTGRES_URL to a direct, SSL-enabled Neon DSN for database nexa_test; "
            "production database settings are never read by this test module"
        ),
    ),
]


@contextmanager
def _isolated_test_database():
    """Run within a uniquely owned schema, always dropping only that schema."""
    admin = Database(POSTGRES_URL)
    try:
        with temporary_owned_schema(admin.engine, new_schema_name()) as schema_name:
            isolated_url = url_for_schema(POSTGRES_URL, schema_name)
            repository = MessageRepository(isolated_url)
            try:
                repository.initialize()
                yield isolated_url, repository
            finally:
                repository.database.dispose()
    finally:
        admin.dispose()


def _request(message_id: str) -> ProcessMessageRequest:
    return ProcessMessageRequest(
        message_id=message_id,
        sender_name="Integration sender",
        original_message="Please review this test message.",
        conversation_context=[],
    )


def _cleanup(repository: MessageRepository, user_id: str, message_ids: list[str]) -> None:
    with repository.database.write() as connection:
        connection.execute(
            delete(messages).where(
                messages.c.user_id == user_id,
                messages.c.message_id.in_(message_ids),
            )
        )
        connection.execute(delete(rate_limit_buckets).where(rate_limit_buckets.c.user_id == user_id))


def _finish(repository: MessageRepository, user_id: str, message_id: str, token: str):
    return repository.complete(
        user_id,
        message_id,
        token,
        {
            "classification": "IMPORTANT",
            "priority_score": 0.9,
            "explanation": "PostgreSQL integration result.",
            "requires_response": True,
            "suggested_response": "I’ll review it.",
        },
    )


def test_postgres_alembic_upgrade_from_empty_isolated_schema():
    with _isolated_test_database() as (_, isolated):
        tables = set(inspect(isolated.database.engine).get_table_names())
        assert {"alembic_version", "whatsapp_ai_messages", "api_rate_limit_buckets"} <= tables
        isolated.initialize()  # Upgrade is repeatable/idempotent.
        assert set(inspect(isolated.database.engine).get_table_names()) == tables


def test_postgres_migration_preserves_legacy_message_and_replaces_identity_key():
    admin = Database(POSTGRES_URL)
    try:
        with temporary_owned_schema(admin.engine, new_schema_name()) as schema_name:
            isolated_url = url_for_schema(POSTGRES_URL, schema_name)
            repository = MessageRepository(isolated_url)
            try:
                with repository.database.write() as connection:
                    connection.execute(
                        text(
                            """CREATE TABLE whatsapp_ai_messages (
                                user_id VARCHAR(200) NOT NULL,
                                message_id VARCHAR(200) NOT NULL,
                                sender_name VARCHAR(120) NOT NULL,
                                original_message TEXT NOT NULL,
                                request_hash VARCHAR(64) NOT NULL,
                                conversation_context_json TEXT NOT NULL,
                                response_language VARCHAR(40) NOT NULL,
                                received_at TIMESTAMP WITH TIME ZONE,
                                classification VARCHAR(20),
                                priority_score FLOAT,
                                explanation VARCHAR(500),
                                requires_response BOOLEAN,
                                suggested_response VARCHAR(1000) NOT NULL DEFAULT '',
                                processing_status VARCHAR(20) NOT NULL,
                                processing_error VARCHAR(250),
                                created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                                updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
                                processed_at TIMESTAMP WITH TIME ZONE,
                                CONSTRAINT pk_whatsapp_ai_messages
                                    PRIMARY KEY (user_id, message_id)
                            )"""
                        )
                    )
                    connection.execute(
                        text(
                            """INSERT INTO whatsapp_ai_messages (
                                user_id, message_id, sender_name, original_message,
                                request_hash, conversation_context_json, response_language,
                                classification, priority_score, explanation, requires_response,
                                suggested_response, processing_status, created_at, updated_at
                            ) VALUES (
                                'legacy-user', 'legacy-pg-id', 'Asha', 'Preserve this',
                                'legacy-hash', '[]', 'en', 'IMPORTANT', 0.8,
                                'Stored result', true, 'Will do', 'COMPLETED',
                                :created_at, :updated_at
                            )"""
                        ),
                        {"created_at": datetime.now(UTC), "updated_at": datetime.now(UTC)},
                    )

                repository.initialize()
                record = repository.get("legacy-user", "legacy-pg-id")
                pk_columns = inspect(repository.database.engine).get_pk_constraint(
                    "whatsapp_ai_messages"
                )["constrained_columns"]
                assert record["original_message"] == "Preserve this"
                assert record["classification"] == "IMPORTANT"
                assert record["suggested_response"] == "Will do"
                assert record["conversation_id"] is None
                assert pk_columns == ["user_id", "conversation_key", "message_id"]
            finally:
                repository.database.dispose()
    finally:
        admin.dispose()


def test_postgres_end_to_end_duplicate_claim_and_completion():
    with _isolated_test_database() as (isolated_url, repository_a):
        repository_b = MessageRepository(isolated_url)
        user_id = f"pg-test-{uuid.uuid4().hex}"
        message_id = f"same-{uuid.uuid4().hex}"
        request = _request(message_id)
        barrier = threading.Barrier(2)

        def claim(repository):
            barrier.wait(timeout=10)
            return repository.claim(user_id, request, 30, retry_max_attempts=3)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = list(pool.map(claim, (repository_a, repository_b)))
            assert sorted((first[0], second[0])) == ["CLAIMED", "PROCESSING"]
            claim_result = first if first[0] == "CLAIMED" else second
            token = claim_result[2]
            assert token
            completed = _finish(repository_a, user_id, message_id, token)
            assert completed["processing_status"] == "COMPLETED"
            duplicate = repository_b.claim(user_id, request, 30, retry_max_attempts=3)
            assert duplicate[0] == "COMPLETED"
            assert duplicate[1]["explanation"] == "PostgreSQL integration result."
            assert repository_a.list(user_id, 10, 0)[1] == 1
        finally:
            _cleanup(repository_a, user_id, [message_id])
            repository_b.database.dispose()


def test_postgres_expired_lease_recovery_and_stale_worker_fencing():
    with _isolated_test_database() as (_, repository):
        user_id = f"pg-lease-{uuid.uuid4().hex}"
        message_id = f"lease-{uuid.uuid4().hex}"
        request = _request(message_id)
        try:
            state, _, old_token = repository.claim(user_id, request, 60)
            assert state == "CLAIMED" and old_token
            with repository.database.write() as connection:
                connection.execute(
                    update(messages)
                    .where(messages.c.user_id == user_id, messages.c.message_id == message_id)
                    .values(lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
                )
            recovered_state, _, new_token = repository.claim(user_id, request, 60)
            assert recovered_state == "CLAIMED" and new_token and new_token != old_token
            assert _finish(repository, user_id, message_id, old_token) is None
            result = _finish(repository, user_id, message_id, new_token)
            assert result["processing_status"] == "COMPLETED"
        finally:
            _cleanup(repository, user_id, [message_id])


def test_postgres_user_isolation_and_rate_limits_shared_between_engines():
    with _isolated_test_database() as (isolated_url, repository_a):
        repository_b = MessageRepository(isolated_url)
        user_a = f"pg-user-a-{uuid.uuid4().hex}"
        user_b = f"pg-user-b-{uuid.uuid4().hex}"
        rate_user = f"pg-rate-{uuid.uuid4().hex}"
        message_id = f"shared-{uuid.uuid4().hex}"
        request = _request(message_id)
        try:
            state_a, _, token_a = repository_a.claim(user_a, request, 30)
            state_b, _, token_b = repository_b.claim(user_b, request, 30)
            assert state_a == state_b == "CLAIMED"
            assert token_a and token_b
            assert _finish(repository_a, user_a, message_id, token_a)
            assert _finish(repository_b, user_b, message_id, token_b)
            assert repository_a.get(user_a, message_id) is not None
            assert repository_a.get(user_b, message_id) is None

            barrier = threading.Barrier(2)

            def consume(repository):
                barrier.wait(timeout=10)
                return repository.consume_rate_limit(rate_user, 1, 60)

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(consume, (repository_a, repository_b)))
            assert results.count(None) == 1
            assert sum(value is not None for value in results) == 1
        finally:
            _cleanup(repository_a, user_a, [message_id])
            _cleanup(repository_b, user_b, [message_id])
            with repository_a.database.write() as connection:
                connection.execute(
                    delete(rate_limit_buckets).where(rate_limit_buckets.c.user_id == rate_user)
                )
            repository_b.database.dispose()


def test_postgres_retry_budget_and_sqlite_migration_integrity(tmp_path: Path):
    source_repository = MessageRepository(tmp_path / "source.sqlite3")
    source_repository.initialize()
    source_user = f"migration-{uuid.uuid4().hex}"
    source_message = f"sqlite-{uuid.uuid4().hex}"
    source_request = _request(source_message)
    source_state, _, source_token = source_repository.claim(source_user, source_request, 30)
    assert source_state == "CLAIMED" and source_token
    _finish(source_repository, source_user, source_message, source_token)

    try:
        with _isolated_test_database() as (isolated_url, repository):
            user_id = f"pg-retry-{uuid.uuid4().hex}"
            message_id = f"retry-{uuid.uuid4().hex}"
            request = _request(message_id)
            try:
                state, _, token = repository.claim(user_id, request, 30, retry_max_attempts=2)
                assert state == "CLAIMED" and token
                cooldown = repository.fail(
                    user_id,
                    message_id,
                    token,
                    "simulated temporary failure",
                    retryable=True,
                    retry_after_seconds=1,
                    retry_max_attempts=2,
                    retry_base_delay_seconds=1,
                    retry_max_delay_seconds=10,
                )
                assert cooldown == 1
                with pytest.raises(RetryCooldown):
                    repository.claim(user_id, request, 30, retry_max_attempts=2)
                with repository.database.write() as connection:
                    connection.execute(
                        update(messages)
                        .where(
                            messages.c.user_id == user_id,
                            messages.c.message_id == message_id,
                        )
                        .values(next_attempt_at=datetime(2000, 1, 1, tzinfo=UTC))
                    )
                retry_state, _, retry_token = repository.claim(
                    user_id, request, 30, retry_max_attempts=2
                )
                assert retry_state == "CLAIMED" and retry_token
                exhausted_cooldown = repository.fail(
                    user_id,
                    message_id,
                    retry_token,
                    "attempt budget exhausted",
                    retryable=True,
                    retry_after_seconds=1,
                    retry_max_attempts=2,
                    retry_base_delay_seconds=1,
                    retry_max_delay_seconds=10,
                )
                assert exhausted_cooldown == 0  # No third attempt is permitted.
                with pytest.raises(RetryLimitReached):
                    repository.claim(user_id, request, 30, retry_max_attempts=2)

                dry_run = migrate_sqlite_to_postgres(
                    source_repository.database_file,
                    isolated_url,
                    dry_run=True,
                )
                assert dry_run["source_messages"] == 1
                migrated = migrate_sqlite_to_postgres(
                    source_repository.database_file,
                    isolated_url,
                    dry_run=False,
                )
                assert migrated["source_messages"] == 1
                assert repository.get(source_user, source_message)["processing_status"] == "COMPLETED"
                assert source_repository.get(source_user, source_message)["processing_status"] == "COMPLETED"
            finally:
                _cleanup(repository, user_id, [message_id])
                _cleanup(repository, source_user, [source_message])
    finally:
        source_repository.database.dispose()
