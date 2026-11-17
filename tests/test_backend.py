import asyncio
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.database.engine import Database
from app.database.schema import messages, rate_limit_buckets
from app.main import create_app
from app.repositories.message_repository import MessageRepository
from app.schemas.messages import (
    AnalysisResult,
    BatchProcessResponse,
    Classification,
    MessageResult,
    ProcessMessageRequest,
)
from app.services.classification import ClassificationOutput, MessageClassifier
from app.services.llm_provider import AIProviderError, OpenAICompatibleProvider
from app.services.reply_generation import MessageAnalyzer, ReplyGenerator, ReplyOutput
from app.tools.migrate_sqlite_to_postgres import _read_source


TOKEN_A = "test-token-for-account-a-123456789"
TOKEN_B = "test-token-for-account-b-987654321"
_USE_PRODUCTION_ANALYZER = object()


class DeterministicTestAnalyzer:
    """Test double only; production analysis always calls the configured LLM."""

    def __init__(self, fail_first: bool = False) -> None:
        self.calls = 0
        self.fail_first = fail_first

    async def analyze(self, request: ProcessMessageRequest) -> AnalysisResult:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise AIProviderError(
                "simulated provider outage", retryable=True, retry_after_seconds=1
            )
        important = "urgent" in request.original_message.lower()
        return AnalysisResult(
            classification=Classification.IMPORTANT if important else Classification.NORMAL,
            priority_score=0.93 if important else 0.18,
            explanation="Test fixture classification based on the test case.",
            requires_response=important,
            suggested_response="I’ll review this shortly." if important else "",
        )


class CountingStructuredProvider:
    """Count classification/reply provider calls without contacting a real LLM."""

    def __init__(self, delay_seconds=0):
        self.calls = 0
        self.delay_seconds = delay_seconds

    async def generate(self, system_prompt, payload, schema):
        self.calls += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if schema is ClassificationOutput:
            return ClassificationOutput(
                classification="IMPORTANT",
                priority_score=0.9,
                explanation="The message requests urgent action.",
                detected_language="English",
                requires_response=True,
            )
        if schema is ReplyOutput:
            return ReplyOutput(suggested_response="I’ll review this today.")
        raise AssertionError(f"Unexpected structured schema: {schema.__name__}")


class AlwaysTransientFailureAnalyzer:
    def __init__(self):
        self.calls = 0

    async def analyze(self, request):
        self.calls += 1
        raise AIProviderError(
            "simulated upstream outage",
            retryable=True,
            retry_after_seconds=1,
            status_code=503,
        )


def make_client(tmp_path, analyzer=None, **settings_overrides):
    settings_values = dict(
        _env_file=None,
        database_path=str(tmp_path / "messages.sqlite3"),
        access_tokens_json={TOKEN_A: "user-a", TOKEN_B: "user-b"},
        llm_api_key=SecretStr(""),
        batch_size_limit=2,
    )
    settings_values.update(settings_overrides)
    settings = Settings(**settings_values)
    repository = MessageRepository(settings.database_file)
    selected_analyzer = (
        DeterministicTestAnalyzer()
        if analyzer is None
        else None if analyzer is _USE_PRODUCTION_ANALYZER else analyzer
    )
    app = create_app(settings, repository, selected_analyzer)
    return TestClient(app)


def message(
    message_id="wa-001",
    text="Can we chat tomorrow?",
    conversation_id=None,
    received_at=None,
):
    return {
        "message_id": message_id,
        "conversation_id": conversation_id,
        "sender_name": "Asha",
        "original_message": text,
        "received_at": received_at or datetime.now(UTC).isoformat(),
        "conversation_context": [],
    }


def auth(token=TOKEN_A):
    return {"Authorization": f"Bearer {token}"}


def test_anonymous_processing_uses_local_user_and_persists_result(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post("/api/v1/messages/process", json=message("local-anon"))
        saved = client.app.state.repository.get("local-development", "local-anon")

    assert response.status_code == 200
    assert response.json()["processing_status"] == "COMPLETED"
    assert response.json()["classification"] == "NORMAL"
    assert set(response.json()) == set(AnalysisResult.model_fields) | {
        "message_id",
        "conversation_id",
        "sender_name",
        "original_message",
        "processing_status",
        "processing_error",
        "received_at",
        "backend_received_at",
        "processing_started_at",
        "ai_response_generated_at",
        "processing_duration_ms",
        "created_at",
        "updated_at",
        "processed_at",
    }
    assert saved is not None
    assert saved["suggested_response"] == ""


def test_anonymous_processing_does_not_require_local_opt_in(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post("/api/v1/messages/process", json=message("no-opt-in"))

    assert response.status_code == 200
    assert response.json()["message_id"] == "no-opt-in"


def test_anonymous_mode_keeps_bearer_auth_and_other_routes_protected(tmp_path):
    with make_client(tmp_path) as client:
        anonymous = client.post(
            "/api/v1/messages/process", json=message("local-anon-auth")
        )
        authenticated = client.post(
            "/api/v1/messages/process",
            headers=auth(TOKEN_B),
            json=message("token-auth", "Hello there"),
        )
        invalid = client.post(
            "/api/v1/messages/process",
            headers=auth("invalid-token"),
            json=message("invalid-token-case"),
        )
        protected_list = client.get("/api/v1/messages")
        openapi = client.app.openapi()

    assert anonymous.status_code == authenticated.status_code == 200
    assert client.app.state.repository.get("user-b", "token-auth") is not None
    assert invalid.status_code == 401
    assert protected_list.status_code == 401
    assert openapi["paths"]["/api/v1/messages/process"]["post"].get("security", []) == []
    assert openapi["paths"]["/api/v1/messages/process/batch"]["post"].get("security", []) == []
    assert openapi["paths"]["/api/v1/messages"]["get"]["security"] == [
        {"HTTPBearer": []}
    ]


def test_processing_auth_requires_bearer_in_production_and_keeps_user_scope(tmp_path):
    with make_client(
        tmp_path,
        environment="production",
        llm_api_key=SecretStr("gsk-test-credential-value-long-enough"),
        database_url=SecretStr(
            "postgresql+psycopg://nexa:some-long-password@db.example:5432/nexa?sslmode=require"
        ),
    ) as client:
        anonymous = client.post("/api/v1/messages/process", json=message("prod-anon"))
        anonymous_batch = client.post(
            "/api/v1/messages/process/batch",
            json={"messages": [message("prod-batch-anon")]},
        )
        malformed = client.post(
            "/api/v1/messages/process",
            headers={"Authorization": "Basic not-a-bearer"},
            json=message("prod-malformed"),
        )
        invalid = client.post(
            "/api/v1/messages/process",
            headers=auth("invalid-token"),
            json=message("prod-invalid"),
        )
        authenticated = client.post(
            "/api/v1/messages/process",
            headers=auth(TOKEN_B),
            json=message("prod-authenticated"),
        )

    assert (
        anonymous.status_code
        == anonymous_batch.status_code
        == malformed.status_code
        == invalid.status_code
        == 401
    )
    assert authenticated.status_code == 200
    assert client.app.state.repository.get("user-b", "prod-authenticated") is not None
    assert client.app.state.repository.get("local-development", "prod-authenticated") is None


def test_batch_processing_succeeds_without_authorization_and_keeps_response_schema(tmp_path):
    payload = {"messages": [message("batch-anon", "Hello there")]}
    with make_client(tmp_path) as client:
        response = client.post("/api/v1/messages/process/batch", json=payload)

    assert response.status_code == 200
    assert set(response.json()) == set(BatchProcessResponse.model_fields)
    assert set(response.json()["results"][0]) == set(MessageResult.model_fields)
    assert response.json()["results"][0]["processing_status"] == "COMPLETED"


def test_important_and_normal_classification_are_saved(tmp_path):
    with make_client(tmp_path) as client:
        urgent = client.post(
            "/api/v1/messages/process", headers=auth(), json=message("wa-u", "Urgent: review today")
        )
        casual = client.post(
            "/api/v1/messages/process", headers=auth(), json=message("wa-n", "Good morning")
        )

    assert urgent.status_code == 200
    assert urgent.json()["classification"] == "IMPORTANT"
    assert urgent.json()["suggested_response"]
    assert casual.status_code == 200
    assert casual.json()["classification"] == "NORMAL"
    assert casual.json()["suggested_response"] == ""
    assert casual.json()["processing_status"] == "COMPLETED"


def test_duplicate_processing_is_idempotent_and_content_conflicts(tmp_path):
    analyzer = DeterministicTestAnalyzer()
    with make_client(tmp_path, analyzer) as client:
        first = client.post("/api/v1/messages/process", headers=auth(), json=message())
        duplicate = client.post("/api/v1/messages/process", headers=auth(), json=message())
        conflict = client.post(
            "/api/v1/messages/process", headers=auth(), json=message(text="Changed source text")
        )

    assert first.status_code == duplicate.status_code == 200
    assert duplicate.json()["message_id"] == "wa-001"
    assert analyzer.calls == 1
    assert conflict.status_code == 409


def test_same_message_id_is_isolated_and_idempotent_per_conversation(tmp_path):
    analyzer = DeterministicTestAnalyzer()
    with make_client(tmp_path, analyzer) as client:
        contact_a = client.post(
            "/api/v1/messages/process",
            headers=auth(),
            json=message("shared-id", "Hello", conversation_id="contact-a"),
        )
        duplicate_a = client.post(
            "/api/v1/messages/process",
            headers=auth(),
            json=message("shared-id", "Hello", conversation_id="contact-a"),
        )
        contact_b = client.post(
            "/api/v1/messages/process",
            headers=auth(),
            json=message("shared-id", "Hello", conversation_id="contact-b"),
        )
        stored_a = client.get(
            "/api/v1/messages/shared-id?conversation_id=contact-a",
            headers=auth(),
        )
        stored_b = client.get(
            "/api/v1/messages/shared-id?conversation_id=contact-b",
            headers=auth(),
        )

    assert contact_a.status_code == duplicate_a.status_code == contact_b.status_code == 200
    assert duplicate_a.json() == contact_a.json()
    assert contact_a.json()["conversation_id"] == "contact-a"
    assert contact_b.json()["conversation_id"] == "contact-b"
    assert stored_a.json()["conversation_id"] == "contact-a"
    assert stored_b.json()["conversation_id"] == "contact-b"
    assert analyzer.calls == 2


def test_same_text_with_distinct_source_ids_remains_two_conversation_messages(tmp_path):
    analyzer = DeterministicTestAnalyzer()
    with make_client(tmp_path, analyzer) as client:
        first = client.post(
            "/api/v1/messages/process",
            headers=auth(),
            json=message(
                "event-one",
                "Same incoming text",
                conversation_id="contact-a",
            ),
        )
        second = client.post(
            "/api/v1/messages/process",
            headers=auth(),
            json=message(
                "event-two",
                "Same incoming text",
                conversation_id="contact-a",
            ),
        )
        records, count = client.app.state.repository.list("user-a", 20, 0)

    assert first.status_code == second.status_code == 200
    assert count == 2
    assert {record["message_id"] for record in records} == {"event-one", "event-two"}
    assert analyzer.calls == 2


def test_ai_context_uses_only_bounded_completed_messages_from_same_conversation(tmp_path):
    class ContextCapturingAnalyzer:
        def __init__(self):
            self.contexts = {}

        async def analyze(self, request):
            self.contexts[request.message_id] = [
                (item.sender_name, item.text) for item in request.conversation_context
            ]
            return AnalysisResult(
                classification=Classification.NORMAL,
                priority_score=0.1,
                explanation="Context isolation fixture.",
                requires_response=False,
                suggested_response="",
            )

    analyzer = ContextCapturingAnalyzer()
    with make_client(
        tmp_path, analyzer, conversation_context_limit=1
    ) as client:
        client.post(
            "/api/v1/messages/process",
            headers=auth(),
            json=message("a-old", "A private history", conversation_id="contact-a"),
        )
        client.post(
            "/api/v1/messages/process",
            headers=auth(),
            json=message("b-old", "B private history", conversation_id="contact-b"),
        )
        client.post(
            "/api/v1/messages/process",
            headers=auth(),
            json=message("a-current", "A current", conversation_id="contact-a"),
        )
        client.post(
            "/api/v1/messages/process",
            headers=auth(),
            json=message("b-current", "B current", conversation_id="contact-b"),
        )

    assert analyzer.contexts["a-current"] == [("Asha", "A private history")]
    assert analyzer.contexts["b-current"] == [("Asha", "B private history")]


def test_identified_conversation_rejects_unscoped_caller_context(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/v1/messages/process",
            headers=auth(),
            json={
                **message("context-mismatch", "Hello", conversation_id="contact-a"),
                "conversation_context": [
                    {"sender_name": "B", "text": "Potentially another contact's data"}
                ],
            },
        )

    assert response.status_code == 422


def test_first_message_invokes_ai_and_creates_one_record(tmp_path):
    provider = CountingStructuredProvider()
    analyzer = MessageAnalyzer(MessageClassifier(provider), ReplyGenerator(provider))
    with make_client(tmp_path, analyzer) as client:
        response = client.post(
            "/api/v1/messages/process",
            headers=auth(),
            json=message("audit-first", "Urgent: please respond today"),
        )
        repository = client.app.state.repository
        _, user_count = repository.list("user-a", 50, 0)

    assert response.status_code == 200
    assert response.json()["processing_status"] == "COMPLETED"
    assert response.json()["detected_language"] == "English"
    assert provider.calls == 2  # one structured classification and one reply request
    assert user_count == 1


def test_sequential_duplicate_returns_saved_result_without_ai_calls(tmp_path):
    provider = CountingStructuredProvider()
    analyzer = MessageAnalyzer(MessageClassifier(provider), ReplyGenerator(provider))
    payload = message("audit-sequential", "Urgent: please respond today")
    with make_client(tmp_path, analyzer) as client:
        first = client.post("/api/v1/messages/process", headers=auth(), json=payload)
        calls_after_first = provider.calls
        duplicate = client.post("/api/v1/messages/process", headers=auth(), json=payload)
        _, user_count = client.app.state.repository.list("user-a", 50, 0)

    assert first.status_code == duplicate.status_code == 200
    assert duplicate.json() == first.json()
    assert calls_after_first == provider.calls == 2
    assert user_count == 1


def test_concurrent_duplicate_requests_claim_once_and_call_ai_once(tmp_path):
    async def scenario():
        provider = CountingStructuredProvider(delay_seconds=0.15)
        analyzer = MessageAnalyzer(MessageClassifier(provider), ReplyGenerator(provider))
        settings = Settings(
            _env_file=None,
            database_path=str(tmp_path / "concurrent.sqlite3"),
            access_tokens_json={TOKEN_A: "user-a", TOKEN_B: "user-b"},
            llm_api_key=SecretStr("test-key"),
        )
        repository = MessageRepository(settings.database_file)
        repository.initialize()
        app = create_app(settings, repository, analyzer)
        payload = message(
            "audit-concurrent",
            "Urgent: please respond today",
            conversation_id="concurrent-contact",
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first, duplicate = await asyncio.gather(
                client.post("/api/v1/messages/process", headers=auth(), json=payload),
                client.post("/api/v1/messages/process", headers=auth(), json=payload),
            )
        _, user_count = repository.list("user-a", 50, 0)
        return first, duplicate, provider.calls, user_count

    first, duplicate, provider_calls, user_count = asyncio.run(scenario())
    assert first.status_code == duplicate.status_code == 200
    assert sorted(
        [first.json()["processing_status"], duplicate.json()["processing_status"]]
    ) == ["COMPLETED", "PROCESSING"]
    assert provider_calls == 2  # exactly one classification + one reply provider call
    assert user_count == 1


def test_concurrent_distinct_messages_in_one_conversation_are_isolated_and_complete(tmp_path):
    async def scenario():
        provider = CountingStructuredProvider(delay_seconds=0.1)
        analyzer = MessageAnalyzer(MessageClassifier(provider), ReplyGenerator(provider))
        settings = Settings(
            _env_file=None,
            database_path=str(tmp_path / "same-conversation-concurrent.sqlite3"),
            access_tokens_json={TOKEN_A: "user-a"},
            llm_api_key=SecretStr("test-key"),
        )
        repository = MessageRepository(settings.database_file)
        repository.initialize()
        app = create_app(settings, repository, analyzer)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/api/v1/messages/process",
                        headers=auth(),
                        json=message(
                            event_id,
                            text,
                            conversation_id="same-conversation",
                        ),
                    )
                    for event_id, text in (
                        ("concurrent-a", "First incoming message"),
                        ("concurrent-b", "Second incoming message"),
                    )
                )
            )
        return responses, provider.calls, repository

    responses, provider_calls, repository = asyncio.run(scenario())
    assert [response.status_code for response in responses] == [200, 200]
    assert [response.json()["processing_status"] for response in responses] == [
        "COMPLETED",
        "COMPLETED",
    ]
    assert provider_calls == 4
    records, count = repository.list("user-a", 10, 0)
    assert count == 2
    assert {record["conversation_id"] for record in records} == {"same-conversation"}


def test_same_message_id_is_independent_for_different_users(tmp_path):
    provider = CountingStructuredProvider()
    analyzer = MessageAnalyzer(MessageClassifier(provider), ReplyGenerator(provider))
    with make_client(tmp_path, analyzer) as client:
        user_a = client.post(
            "/api/v1/messages/process",
            headers=auth(TOKEN_A),
            json=message("audit-shared-id", "Urgent: please respond today"),
        )
        user_b = client.post(
            "/api/v1/messages/process",
            headers=auth(TOKEN_B),
            json=message("audit-shared-id", "Urgent: please respond today"),
        )
        _, user_a_count = client.app.state.repository.list("user-a", 50, 0)
        _, user_b_count = client.app.state.repository.list("user-b", 50, 0)

    assert user_a.status_code == user_b.status_code == 200
    assert user_a.json()["processing_status"] == user_b.json()["processing_status"] == "COMPLETED"
    assert provider.calls == 4
    assert user_a_count == user_b_count == 1


def _expire_lease(database_file, message_id, user_id="user-a"):
    expired = datetime(2000, 1, 1, tzinfo=UTC).isoformat()
    with sqlite3.connect(database_file) as connection:
        connection.execute(
            "UPDATE whatsapp_ai_messages SET lease_expires_at=? WHERE user_id=? AND message_id=?",
            (expired, user_id, message_id),
        )


def test_expired_processing_lease_is_atomically_reclaimed(tmp_path):
    provider = CountingStructuredProvider()
    analyzer = MessageAnalyzer(MessageClassifier(provider), ReplyGenerator(provider))
    payload = message("audit-expired", "Urgent: please respond today")
    with make_client(tmp_path, analyzer) as client:
        request = ProcessMessageRequest.model_validate(payload)
        state, _, token = client.app.state.repository.claim("user-a", request, 180)
        assert state == "CLAIMED" and token is not None
        _expire_lease(client.app.state.repository.database_file, "audit-expired")
        reclaimed = client.post("/api/v1/messages/process", headers=auth(), json=payload)
        _, count = client.app.state.repository.list("user-a", 50, 0)

    assert reclaimed.status_code == 200
    assert reclaimed.json()["processing_status"] == "COMPLETED"
    assert provider.calls == 2
    assert count == 1


def test_live_worker_renews_lease_during_slow_provider_calls(tmp_path):
    async def scenario():
        provider = CountingStructuredProvider(delay_seconds=0.7)
        analyzer = MessageAnalyzer(MessageClassifier(provider), ReplyGenerator(provider))
        settings = Settings(
            _env_file=None,
            database_path=str(tmp_path / "heartbeat.sqlite3"),
            access_tokens_json={TOKEN_A: "user-a"},
            processing_lease_seconds=1.2,
            llm_api_key=SecretStr("test-key"),
        )
        repository = MessageRepository(settings.database_file)
        repository.initialize()
        app = create_app(settings, repository, analyzer)
        payload = message("audit-heartbeat", "Urgent: please respond today")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            first_task = asyncio.create_task(
                client.post("/api/v1/messages/process", headers=auth(), json=payload)
            )
            await asyncio.sleep(1.3)  # Longer than the initial lease; heartbeat must renew it.
            duplicate = await client.post(
                "/api/v1/messages/process", headers=auth(), json=payload
            )
            first = await first_task
        return first, duplicate, provider.calls

    first, duplicate, provider_calls = asyncio.run(scenario())
    assert first.status_code == duplicate.status_code == 200
    assert duplicate.json()["processing_status"] == "PROCESSING"
    assert first.json()["processing_status"] == "COMPLETED"
    assert provider_calls == 2


def test_backend_crash_after_claim_recovers_after_lease_expiry(tmp_path):
    provider = CountingStructuredProvider()
    analyzer = MessageAnalyzer(MessageClassifier(provider), ReplyGenerator(provider))
    payload = ProcessMessageRequest.model_validate(
        message("audit-crash", "Urgent: please respond today")
    )
    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "crash.sqlite3"),
        access_tokens_json={TOKEN_A: "user-a"},
        llm_api_key=SecretStr("test-key"),
    )
    repository = MessageRepository(settings.database_file)
    repository.initialize()

    state, claimed, stale_token = repository.claim("user-a", payload, 180)
    assert state == "CLAIMED"
    assert claimed["processing_status"] == "PROCESSING"
    assert provider.calls == 0  # Simulates process death immediately after DB claim.
    _expire_lease(settings.database_file, "audit-crash")

    app = create_app(settings, repository, analyzer)
    with TestClient(app) as client:
        recovered = client.post(
            "/api/v1/messages/process",
            headers=auth(),
            json=payload.model_dump(mode="json"),
        )

    assert recovered.status_code == 200
    assert recovered.json()["processing_status"] == "COMPLETED"
    assert provider.calls == 2
    assert stale_token is not None


def test_expired_worker_is_fenced_from_saving_after_reclaim(tmp_path):
    payload = ProcessMessageRequest.model_validate(
        message("audit-fence", "Urgent: please respond today")
    )
    repository = MessageRepository(tmp_path / "fencing.sqlite3")
    repository.initialize()
    first_state, _, old_token = repository.claim("user-a", payload, 180)
    assert first_state == "CLAIMED"
    assert old_token is not None

    _expire_lease(repository.database_file, "audit-fence")
    second_state, _, new_token = repository.claim("user-a", payload, 180)
    assert second_state == "CLAIMED"
    assert new_token is not None and new_token != old_token

    analysis = {
        "classification": "IMPORTANT",
        "priority_score": 0.9,
        "explanation": "Current worker result.",
        "requires_response": True,
        "suggested_response": "Current worker reply.",
    }
    stale_save = repository.complete("user-a", "audit-fence", old_token, analysis)
    current_save = repository.complete("user-a", "audit-fence", new_token, analysis)
    saved = repository.get("user-a", "audit-fence")

    assert stale_save is None
    assert current_save is not None
    assert saved is not None
    assert saved["processing_status"] == "COMPLETED"
    assert saved["explanation"] == "Current worker result."


def test_existing_database_is_migrated_additively_and_data_is_preserved(tmp_path):
    database_file = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_file) as connection:
        connection.execute(
            """CREATE TABLE whatsapp_ai_messages (
                user_id TEXT NOT NULL, message_id TEXT NOT NULL, sender_name TEXT NOT NULL,
                original_message TEXT NOT NULL, request_hash TEXT NOT NULL,
                conversation_context_json TEXT NOT NULL, response_language TEXT NOT NULL,
                received_at TEXT, classification TEXT, priority_score REAL, explanation TEXT,
                requires_response INTEGER, suggested_response TEXT NOT NULL DEFAULT '',
                processing_status TEXT NOT NULL, processing_error TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, processed_at TEXT,
                PRIMARY KEY (user_id, message_id)
            )"""
        )
        connection.execute(
            """INSERT INTO whatsapp_ai_messages (
                user_id, message_id, sender_name, original_message, request_hash,
                conversation_context_json, response_language, processing_status,
                created_at, updated_at
            ) VALUES ('user-a', 'legacy-id', 'Asha', 'Hello', 'hash', '[]', 'en',
                      'PROCESSING', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"""
        )

    repository = MessageRepository(database_file)
    repository.initialize()
    with sqlite3.connect(database_file) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(whatsapp_ai_messages)")}
        retained = connection.execute(
            "SELECT original_message, processing_status FROM whatsapp_ai_messages "
            "WHERE user_id='user-a' AND message_id='legacy-id'"
        ).fetchone()

    assert {
        "processing_started_at",
        "lease_expires_at",
        "claim_token",
        "attempt_count",
        "retryable",
        "next_attempt_at",
        "detected_language",
        "conversation_id",
        "conversation_key",
        "backend_received_at",
        "ai_response_generated_at",
    } <= columns
    assert retained == ("Hello", "PROCESSING")
    with sqlite3.connect(database_file) as connection:
        primary_key = [
            row[1]
            for row in sorted(
                (row for row in connection.execute(
                    "PRAGMA table_info(whatsapp_ai_messages)"
                ) if row[5]),
                key=lambda row: row[5],
            )
        ]
        conversation_key = connection.execute(
            "SELECT conversation_key FROM whatsapp_ai_messages "
            "WHERE user_id='user-a' AND message_id='legacy-id'"
        ).fetchone()
    assert primary_key == ["user_id", "conversation_key", "message_id"]
    assert conversation_key == (
        hashlib.sha256(b"legacy:legacy-id").hexdigest(),
    )


@pytest.mark.parametrize(
    "source_time",
    ["2026-11-14T18:30:00+05:30", "2026-11-14T08:30:00-04:30"],
)
def test_offset_timestamp_is_normalized_and_processing_timestamps_round_trip(
    tmp_path, source_time
):
    payload = message(
        "timestamp-round-trip",
        "Please respond",
        received_at=source_time,
    )
    request = ProcessMessageRequest.model_validate(payload)
    assert request.received_at == datetime(2026, 11, 14, 13, 0, tzinfo=UTC)
    provider = CountingStructuredProvider()
    analyzer = MessageAnalyzer(MessageClassifier(provider), ReplyGenerator(provider))

    with make_client(tmp_path, analyzer) as client:
        response = client.post(
            "/api/v1/messages/process", headers=auth(), json=payload
        )
        saved = client.app.state.repository.get("user-a", "timestamp-round-trip")
        naive_timestamp = client.post(
            "/api/v1/messages/process",
            headers=auth(),
            json=message(
                "naive-timestamp",
                "Please respond",
                received_at="2026-11-14T18:30:00",
            ),
        )

    assert response.status_code == 200
    assert naive_timestamp.status_code == 422
    assert response.json()["received_at"] == "2026-11-14T13:00:00Z"
    assert response.json()["backend_received_at"].endswith("Z")
    assert response.json()["processing_started_at"].endswith("Z")
    assert response.json()["processed_at"].endswith("Z")
    assert response.json()["ai_response_generated_at"].endswith("Z")
    assert response.json()["processing_duration_ms"] >= 0
    started_at = datetime.fromisoformat(
        response.json()["processing_started_at"].replace("Z", "+00:00")
    )
    processed_at = datetime.fromisoformat(
        response.json()["processed_at"].replace("Z", "+00:00")
    )
    assert response.json()["processing_duration_ms"] == int(
        (processed_at - started_at).total_seconds() * 1000
    )
    assert (
        saved["backend_received_at"].isoformat().replace("+00:00", "Z")
        == response.json()["backend_received_at"]
    )


def test_concurrent_same_message_id_in_different_conversations_processes_separately(tmp_path):
    async def scenario():
        provider = CountingStructuredProvider(delay_seconds=0.1)
        analyzer = MessageAnalyzer(MessageClassifier(provider), ReplyGenerator(provider))
        settings = Settings(
            _env_file=None,
            database_path=str(tmp_path / "conversation-concurrent.sqlite3"),
            access_tokens_json={TOKEN_A: "user-a"},
            llm_api_key=SecretStr("test-key"),
        )
        repository = MessageRepository(settings.database_file)
        repository.initialize()
        app = create_app(settings, repository, analyzer)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            results = await asyncio.gather(
                client.post(
                    "/api/v1/messages/process",
                    headers=auth(),
                    json=message(
                        "same-source-id",
                        "Same text",
                        conversation_id="contact-a",
                    ),
                ),
                client.post(
                    "/api/v1/messages/process",
                    headers=auth(),
                    json=message(
                        "same-source-id",
                        "Same text",
                        conversation_id="contact-b",
                    ),
                ),
            )
        return results, provider.calls, repository

    results, provider_calls, repository = asyncio.run(scenario())
    assert [response.status_code for response in results] == [200, 200]
    assert [response.json()["processing_status"] for response in results] == [
        "COMPLETED",
        "COMPLETED",
    ]
    assert provider_calls == 4
    assert repository.get("user-a", "same-source-id", "contact-a") is not None
    assert repository.get("user-a", "same-source-id", "contact-b") is not None


def test_postgres_engine_configuration_and_portable_schema_compile_without_connecting():
    database = Database(
        "postgresql+psycopg://user:password@db.example:5432/nexa?sslmode=require"
    )
    try:
        assert database.engine.dialect.name == "postgresql"
        assert database.engine.pool.size() == 5
        assert list(messages.primary_key.columns.keys()) == [
            "user_id",
            "conversation_key",
            "message_id",
        ]
        assert list(rate_limit_buckets.primary_key.columns.keys()) == [
            "user_id",
            "window_start",
        ]
    finally:
        database.dispose()


def test_sqlite_export_reader_preserves_message_fields_and_rate_buckets(tmp_path):
    repository = MessageRepository(tmp_path / "export-source.sqlite3")
    repository.initialize()
    user_id = "export-user"
    message_id = "export-message"
    payload = ProcessMessageRequest.model_validate(
        message(message_id, "Please preserve this source record")
    )
    state, _, token = repository.claim(user_id, payload, 90)
    assert state == "CLAIMED" and token
    completed = repository.complete(
        user_id,
        message_id,
        token,
        {
            "classification": "IMPORTANT",
            "priority_score": 0.8,
            "explanation": "Migration fixture result.",
            "requires_response": True,
            "suggested_response": "Will do.",
        },
    )
    with repository.database.write() as connection:
        connection.exec_driver_sql(
            "INSERT INTO api_rate_limit_buckets(user_id, window_start, request_count) "
            "VALUES (?, ?, ?)",
            (user_id, 123456, 7),
        )

    rows, rate_rows = _read_source(repository.database_file)
    migrated = rows[0]
    assert completed["processing_status"] == "COMPLETED"
    assert migrated["message_id"] == message_id
    assert migrated["classification"] == "IMPORTANT"
    assert migrated["suggested_response"] == "Will do."
    assert migrated["processing_status"] == "COMPLETED"
    assert migrated["attempt_count"] == 1
    assert migrated["claim_token"] is None
    assert rate_rows == [{"user_id": user_id, "window_start": 123456, "request_count": 7}]
    assert repository.get(user_id, message_id)["processing_status"] == "COMPLETED"


def test_concurrent_database_initialization_serializes_migrations(tmp_path):
    database_file = tmp_path / "parallel-init.sqlite3"

    def initialize_once(_):
        MessageRepository(database_file).initialize()

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(initialize_once, range(4)))

    repository = MessageRepository(database_file)
    repository.check_ready()


def test_retention_removes_old_rows_but_preserves_live_processing(tmp_path):
    repository = MessageRepository(tmp_path / "retention.sqlite3")
    repository.initialize()
    current = datetime.now(UTC)
    old = (current.replace(year=current.year - 1)).isoformat()
    completed_request = ProcessMessageRequest.model_validate(
        message("retention-completed", "Old message")
    )
    active_request = ProcessMessageRequest.model_validate(
        message("retention-active", "Active message")
    )
    _, _, completed_token = repository.claim("user-a", completed_request, 180)
    _, _, active_token = repository.claim("user-a", active_request, 180)
    assert completed_token and active_token
    repository.complete(
        "user-a",
        completed_request.message_id,
        completed_token,
        {
            "classification": "NORMAL",
            "priority_score": 0.1,
            "explanation": "Old result.",
            "requires_response": False,
            "suggested_response": "",
        },
    )
    with sqlite3.connect(repository.database_file) as connection:
        connection.execute(
            "UPDATE whatsapp_ai_messages SET updated_at=? WHERE message_id='retention-completed'",
            (old,),
        )
        connection.execute(
            "UPDATE whatsapp_ai_messages SET updated_at=? WHERE message_id='retention-active'",
            (old,),
        )

    deleted = repository.purge_expired_messages(90, now=current)
    assert deleted == 1
    assert repository.get("user-a", "retention-completed") is None
    assert repository.get("user-a", "retention-active")["processing_status"] == "PROCESSING"


def test_completed_record_is_not_reprocessed_even_after_lease_time(tmp_path):
    provider = CountingStructuredProvider()
    analyzer = MessageAnalyzer(MessageClassifier(provider), ReplyGenerator(provider))
    payload = message("audit-completed", "Urgent: please respond today")
    with make_client(tmp_path, analyzer) as client:
        first = client.post("/api/v1/messages/process", headers=auth(), json=payload)
        _expire_lease(client.app.state.repository.database_file, "audit-completed")
        repeated = client.post("/api/v1/messages/process", headers=auth(), json=payload)

    assert first.status_code == repeated.status_code == 200
    assert first.json() == repeated.json()
    assert provider.calls == 2


def test_user_data_is_scoped_to_authenticated_token(tmp_path):
    with make_client(tmp_path) as client:
        saved = client.post("/api/v1/messages/process", headers=auth(TOKEN_A), json=message())
        invisible = client.get("/api/v1/messages/wa-001", headers=auth(TOKEN_B))
        unauthenticated = client.get("/api/v1/messages/wa-001")

    assert saved.status_code == 200
    assert invisible.status_code == 404
    assert unauthenticated.status_code == 401


def test_retry_after_provider_failure(tmp_path):
    analyzer = DeterministicTestAnalyzer(fail_first=True)
    with make_client(tmp_path, analyzer) as client:
        failed = client.post("/api/v1/messages/process", headers=auth(), json=message())
        stored = client.get("/api/v1/messages/wa-001", headers=auth())
        original_received_at = stored.json()["received_at"]
        original_backend_received_at = stored.json()["backend_received_at"]
        original_created_at = stored.json()["created_at"]
        with sqlite3.connect(client.app.state.repository.database_file) as connection:
            connection.execute(
                "UPDATE whatsapp_ai_messages SET next_attempt_at=? WHERE user_id='user-a'",
                ("2000-01-01T00:00:00+00:00",),
            )
        retried = client.post("/api/v1/messages/wa-001/retry", headers=auth())

    assert failed.status_code == 503
    assert failed.headers["retry-after"] == "5"
    assert stored.json()["processing_status"] == "FAILED"
    assert retried.status_code == 200
    assert retried.json()["processing_status"] == "COMPLETED"
    assert retried.json()["received_at"] == original_received_at
    assert retried.json()["backend_received_at"] == original_backend_received_at
    assert retried.json()["created_at"] == original_created_at
    assert analyzer.calls == 2


def test_identified_conversation_retry_uses_the_same_conversation_scope(tmp_path):
    analyzer = DeterministicTestAnalyzer(fail_first=True)
    conversation_id = "retry-contact"
    payload = message(
        "retry-conversation-message",
        "Please respond",
        conversation_id=conversation_id,
    )
    with make_client(tmp_path, analyzer) as client:
        failed = client.post(
            "/api/v1/messages/process", headers=auth(), json=payload
        )
        with sqlite3.connect(client.app.state.repository.database_file) as connection:
            connection.execute(
                "UPDATE whatsapp_ai_messages SET next_attempt_at=? WHERE user_id='user-a'",
                ("2000-01-01T00:00:00+00:00",),
            )
        retried = client.post(
            "/api/v1/messages/retry-conversation-message/retry",
            headers=auth(),
            params={"conversation_id": conversation_id},
        )

    assert failed.status_code == 503
    assert retried.status_code == 200
    assert retried.json()["processing_status"] == "COMPLETED"
    assert retried.json()["conversation_id"] == conversation_id
    assert analyzer.calls == 2


def test_batch_limit_and_health_endpoint(tmp_path):
    with make_client(tmp_path) as client:
        health = client.get("/api/v1/health")
        oversized = client.post(
            "/api/v1/messages/process/batch",
            headers=auth(),
            json={"messages": [message("a"), message("b"), message("c")]},
        )

    assert health.json() == {"status": "ok"}
    assert oversized.status_code == 413


def test_per_user_rate_limit_returns_retry_after_and_isolates_quotas(tmp_path):
    with make_client(
        tmp_path,
        rate_limit_requests=2,
        rate_limit_window_seconds=60,
    ) as client:
        first = client.get("/api/v1/messages", headers=auth(TOKEN_A))
        second = client.get("/api/v1/messages", headers=auth(TOKEN_A))
        limited = client.get("/api/v1/messages", headers=auth(TOKEN_A))
        other_user = client.get("/api/v1/messages", headers=auth(TOKEN_B))

    assert first.status_code == second.status_code == 200
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
    assert other_user.status_code == 200


def test_batch_rate_limit_charges_each_message(tmp_path):
    with make_client(
        tmp_path,
        rate_limit_requests=2,
        rate_limit_window_seconds=60,
    ) as client:
        batch = client.post(
            "/api/v1/messages/process/batch",
            headers=auth(),
            json={"messages": [message("batch-rate-a"), message("batch-rate-b")]},
        )
        next_request = client.get("/api/v1/messages", headers=auth())

    assert batch.status_code == 200
    assert next_request.status_code == 429


def test_production_settings_require_secrets_and_valid_groq_configuration():
    with pytest.raises(RuntimeError):
        Settings(_env_file=None, environment="production")

    valid = Settings(
        _env_file=None,
        environment="production",
        llm_base_url="https://api.groq.com/openai/v1",
        llm_model="openai/gpt-oss-120b",
        llm_api_key=SecretStr("gsk-test-credential-value-long-enough"),
        database_url=SecretStr(
            "postgresql+psycopg://nexa:some-long-password@db.example:5432/nexa?sslmode=require"
        ),
        access_tokens_json={"a" * 40: "android-user"},
    )
    assert valid.environment == "production"

    with pytest.raises(RuntimeError):
        Settings(
            _env_file=None,
            environment="production",
            llm_base_url="http://api.groq.com/openai/v1",
            llm_api_key=SecretStr("gsk-test-credential-value-long-enough"),
            access_tokens_json={"a" * 40: "android-user"},
        )

    with pytest.raises(RuntimeError) as invalid_url:
        Settings(_env_file=None, llm_base_url="not-a-url")
    assert "not-a-url" not in str(invalid_url.value)

    with pytest.raises(RuntimeError):
        Settings(_env_file=None, llm_model="model name with spaces")

    sensitive_token = "sensitive-token-that-must-not-be-echoed"
    with pytest.raises(RuntimeError) as missing_key:
        Settings(
            _env_file=None,
            environment="production",
            access_tokens_json={sensitive_token: "android-user"},
        )
    assert sensitive_token not in str(missing_key.value)


def test_readiness_endpoint_checks_database_without_exposing_details(tmp_path):
    with make_client(tmp_path) as client:
        ready = client.get("/api/v1/ready")
        client.app.state.repository.check_ready = lambda: (_ for _ in ()).throw(
            sqlite3.OperationalError("private database path")
        )
        unavailable = client.get("/api/v1/ready")

    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert unavailable.status_code == 503
    assert "private database path" not in unavailable.text


def test_request_validation_response_does_not_echo_private_message_content(tmp_path):
    private_marker = "PRIVATE-WHATSAPP-CONTENT-MARKER"
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/v1/messages/process",
            json={"message_id": "invalid", "sender_name": "Asha", "original_message": private_marker, "unexpected": True},
        )
        invalid_batch = client.post(
            "/api/v1/messages/process/batch",
            json={"messages": [{"message_id": "invalid-batch"}]},
        )

    assert response.status_code == 422
    assert private_marker not in response.text
    assert invalid_batch.status_code == 422


def test_retry_cooldown_and_attempt_limit_bound_provider_calls(tmp_path):
    analyzer = AlwaysTransientFailureAnalyzer()
    with make_client(
        tmp_path,
        analyzer,
        retry_max_attempts=3,
        retry_base_delay_seconds=1,
        retry_max_delay_seconds=10,
    ) as client:
        payload = message("retry-limit", "Please analyze")
        first = client.post("/api/v1/messages/process", headers=auth(), json=payload)
        cooldown = client.post("/api/v1/messages/process", headers=auth(), json=payload)
        with sqlite3.connect(client.app.state.repository.database_file) as connection:
            connection.execute(
                "UPDATE whatsapp_ai_messages SET next_attempt_at=? WHERE user_id='user-a'",
                ("2000-01-01T00:00:00+00:00",),
            )
        second = client.post("/api/v1/messages/process", headers=auth(), json=payload)
        with sqlite3.connect(client.app.state.repository.database_file) as connection:
            connection.execute(
                "UPDATE whatsapp_ai_messages SET next_attempt_at=? WHERE user_id='user-a'",
                ("2000-01-01T00:00:00+00:00",),
            )
        third = client.post("/api/v1/messages/process", headers=auth(), json=payload)
        exhausted = client.post("/api/v1/messages/process", headers=auth(), json=payload)

    assert first.status_code == second.status_code == 503
    assert third.status_code == 502
    assert "retry-after" not in third.headers
    assert first.headers["retry-after"] == "1"
    assert second.headers["retry-after"] == "2"
    assert cooldown.status_code == 429
    assert int(cooldown.headers["retry-after"]) > 0
    assert exhausted.status_code == 409
    assert analyzer.calls == 3


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(429, True), (503, True), (401, False)],
)
def test_provider_http_errors_are_classified_and_retry_after_is_parsed(
    status_code, retryable, caplog
):
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code,
                headers={"Retry-After": "11"},
                json={"error": "PRIVATE-PROVIDER-RESPONSE"},
            )

        provider = OpenAICompatibleProvider(
            Settings(_env_file=None, llm_api_key=SecretStr("test-secret")),
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(AIProviderError) as raised:
            await provider.generate("system", {}, AnalysisResult)
        return raised.value

    error = asyncio.run(scenario())
    assert error.status_code == status_code
    assert error.retryable is retryable
    assert error.retry_after_seconds == 11
    assert "PRIVATE-PROVIDER-RESPONSE" not in caplog.text
    assert "test-secret" not in caplog.text


def test_provider_connection_timeout_is_retryable_and_redacted(caplog):
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("PRIVATE-CONNECTION-DETAIL")

        provider = OpenAICompatibleProvider(
            Settings(_env_file=None, llm_api_key=SecretStr("test-secret")),
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(AIProviderError) as raised:
            await provider.generate("system", {}, AnalysisResult)
        return raised.value

    error = asyncio.run(scenario())
    assert error.retryable is True
    assert "PRIVATE-CONNECTION-DETAIL" not in str(error)
    assert "PRIVATE-CONNECTION-DETAIL" not in caplog.text


def test_provider_429_is_returned_with_retry_after_without_provider_details(tmp_path):
    class RateLimitedAnalyzer:
        async def analyze(self, request):
            raise AIProviderError(
                "private upstream details",
                retryable=True,
                retry_after_seconds=13,
                status_code=429,
            )

    with make_client(tmp_path, RateLimitedAnalyzer()) as client:
        response = client.post(
            "/api/v1/messages/process", headers=auth(), json=message("upstream-429")
        )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "13"
    assert "private upstream details" not in response.text


def test_empty_payload_and_invalid_analysis_json_are_rejected():
    with pytest.raises(ValidationError):
        ProcessMessageRequest.model_validate(message(text=""))
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate_json('{"classification":"URGENT"}')


def test_backend_unavailable_is_marked_failed(tmp_path):
    with make_client(tmp_path, _USE_PRODUCTION_ANALYZER) as client:
        response = client.post("/api/v1/messages/process", headers=auth(), json=message())
        saved = client.get("/api/v1/messages/wa-001", headers=auth())

    assert response.status_code == 502
    assert saved.json()["processing_status"] == "FAILED"
    assert "provider" in saved.json()["processing_error"].lower()


def test_invalid_provider_json_is_rejected_without_leaking_body():
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "not-json"}}]},
            )

        provider = OpenAICompatibleProvider(
            Settings(_env_file=None, llm_api_key=SecretStr("test-secret")),
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(AIProviderError, match="invalid structured response"):
            await provider.generate(
                "strict JSON", {}, AnalysisResult
            )

    asyncio.run(scenario())


def test_reply_generation_uses_structured_output_and_skips_unneeded_replies():
    class StructuredProvider:
        def __init__(self):
            self.schemas = []

        async def generate(self, system_prompt, payload, schema):
            self.schemas.append(schema)
            if schema is ClassificationOutput:
                return ClassificationOutput(
                    classification="IMPORTANT",
                    priority_score=0.8,
                    explanation="A reply was requested.",
                    detected_language="Telugu-English mixed",
                    requires_response=True,
                )
            return ReplyOutput(suggested_response="Could you share the deadline?")

    async def scenario():
        provider = StructuredProvider()
        analyzer = MessageAnalyzer(
            MessageClassifier(provider), ReplyGenerator(provider)
        )
        result = await analyzer.analyze(
            ProcessMessageRequest.model_validate(
                message(text="Can you confirm the deadline?")
            )
        )
        assert result.suggested_response == "Could you share the deadline?"
        assert result.ai_response_generated_at is not None
        assert result.detected_language == "Telugu-English mixed"
        assert provider.schemas == [ClassificationOutput, ReplyOutput]

        provider.schemas.clear()

        class NoReplyClassifier:
            async def classify(self, request):
                return AnalysisResult(
                    classification=Classification.NORMAL,
                    priority_score=0.1,
                    explanation="No response is needed.",
                    requires_response=False,
                    suggested_response="",
                )

        no_reply = MessageAnalyzer(
            NoReplyClassifier(), ReplyGenerator(provider)
        )
        result = await no_reply.analyze(
            ProcessMessageRequest.model_validate(message(text="FYI: office is closed"))
        )
        assert result.suggested_response == ""
        assert result.ai_response_generated_at is None
        assert provider.schemas == []

    asyncio.run(scenario())
