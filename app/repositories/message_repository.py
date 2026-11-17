from __future__ import annotations

import hashlib
import json
import math
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.database.engine import Database
from app.database.schema import messages, rate_limit_buckets
from app.schemas.messages import ContextMessage, ProcessMessageRequest


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    raw_context = result.pop("conversation_context_json", "[]")
    result["conversation_context"] = json.loads(raw_context or "[]")
    received_at = result.get("received_at")
    if (
        isinstance(received_at, datetime)
        and received_at.tzinfo is None
        and result.get("backend_received_at") is not None
    ):
        result["received_at"] = received_at.replace(tzinfo=UTC)
    return result


class DuplicateMessageConflict(Exception):
    """A stable message identifier was reused for different content."""


class RetryCooldown(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = max(1, retry_after_seconds)


class RetryLimitReached(Exception):
    """The message has no remaining retry attempts."""


class MessageRepository:
    def __init__(self, database: Database | Path | str) -> None:
        if isinstance(database, Database):
            self.database = database
        elif isinstance(database, Path):
            self.database = Database(database.expanduser().resolve())
        elif "://" in database:
            self.database = Database(database)
        else:
            self.database = Database(Path(database).expanduser().resolve())
        try:
            self.database_file: Path | None = self.database.database_file
        except RuntimeError:
            self.database_file = None

    def initialize(self) -> None:
        self.database.migrate()

    def claim(
        self,
        user_id: str,
        request: ProcessMessageRequest,
        lease_seconds: float,
        retry_max_attempts: int = 3,
        conversation_context_limit: int = 10,
    ) -> tuple[str, dict[str, Any], str | None]:
        now = utc_now()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        claim_token = uuid4().hex
        conversation_key = self._conversation_key(request.conversation_id, request.message_id)
        values = {
            "user_id": user_id,
            "conversation_key": conversation_key,
            "conversation_id": request.conversation_id,
            "message_id": request.message_id,
            "sender_name": request.sender_name,
            "original_message": request.original_message,
            "request_hash": self._content_hash(request),
            "conversation_context_json": "[]",
            "response_language": request.response_language,
            "received_at": request.received_at,
            "backend_received_at": now,
            "processing_status": "PROCESSING",
            "processing_started_at": now,
            "lease_expires_at": lease_expires_at,
            "claim_token": claim_token,
            "attempt_count": 1,
            "retryable": True,
            "created_at": now,
            "updated_at": now,
        }
        with self.database.write() as connection:
            insert_statement = self._insert_ignore_conflict(
                connection,
                messages,
                values,
                (
                    messages.c.user_id,
                    messages.c.conversation_key,
                    messages.c.message_id,
                ),
            )
            inserted = connection.execute(insert_statement).rowcount == 1
            row = self._locked_message(
                connection, user_id, conversation_key, request.message_id
            )
            if row is None:
                raise RuntimeError("Message claim could not be read after insert")
            existing = _as_dict(row)
            if inserted:
                context = self._conversation_context(
                    connection,
                    user_id,
                    conversation_key,
                    request,
                    conversation_context_limit,
                )
                context_json = json.dumps(
                    [item.model_dump(mode="json") for item in context],
                    ensure_ascii=False,
                )
                connection.execute(
                    sa.update(messages)
                    .where(
                        messages.c.user_id == user_id,
                        messages.c.conversation_key == conversation_key,
                        messages.c.message_id == request.message_id,
                        messages.c.claim_token == claim_token,
                    )
                    .values(conversation_context_json=context_json)
                )
                inserted_row = self._locked_message(
                    connection, user_id, conversation_key, request.message_id
                )
                return "CLAIMED", self._public_record(_as_dict(inserted_row)), claim_token
            if existing["request_hash"] != values["request_hash"]:
                raise DuplicateMessageConflict
            state = existing["processing_status"]
            if state == "COMPLETED":
                return state, self._public_record(existing), None
            if state == "PROCESSING" and self._lease_is_active(
                existing.get("lease_expires_at"), now
            ):
                return state, self._public_record(existing), None

            attempt_count = int(existing.get("attempt_count") or 0)
            if state == "FAILED":
                if not bool(existing.get("retryable")) or attempt_count >= retry_max_attempts:
                    raise RetryLimitReached
                next_attempt_at = existing.get("next_attempt_at")
                if next_attempt_at:
                    retry_after = self._retry_after_seconds(next_attempt_at, now)
                    if retry_after > 0:
                        raise RetryCooldown(retry_after)
            if state == "PROCESSING" and attempt_count >= retry_max_attempts:
                connection.execute(
                    sa.update(messages)
                    .where(
                        messages.c.user_id == user_id,
                        messages.c.conversation_key == conversation_key,
                        messages.c.message_id == request.message_id,
                    )
                    .values(
                        processing_status="FAILED",
                        processing_error="Maximum processing attempts reached",
                        retryable=False,
                        lease_expires_at=None,
                        claim_token=None,
                        updated_at=now,
                    )
                )
                failed = self._locked_message(
                    connection, user_id, conversation_key, request.message_id
                )
                return "FAILED", self._public_record(_as_dict(failed)), None

            connection.execute(
                sa.update(messages)
                .where(
                    messages.c.user_id == user_id,
                    messages.c.conversation_key == conversation_key,
                    messages.c.message_id == request.message_id,
                )
                .values(
                    processing_status="PROCESSING",
                    processing_error=None,
                    response_language=request.response_language,
                    processing_started_at=now,
                    lease_expires_at=lease_expires_at,
                    claim_token=claim_token,
                    attempt_count=attempt_count + 1,
                    retryable=True,
                    next_attempt_at=None,
                    updated_at=now,
                )
            )
            updated = self._locked_message(
                connection, user_id, conversation_key, request.message_id
            )
            return "CLAIMED", self._public_record(_as_dict(updated)), claim_token

    def complete(
        self,
        user_id: str,
        message_id: str,
        claim_token: str,
        analysis: dict[str, Any],
        conversation_id: str | None = None,
    ) -> dict[str, Any] | None:
        now = utc_now()
        conversation_key = self._conversation_key(conversation_id, message_id)
        with self.database.write() as connection:
            result = connection.execute(
                sa.update(messages)
                .where(
                    messages.c.user_id == user_id,
                    messages.c.conversation_key == conversation_key,
                    messages.c.message_id == message_id,
                    messages.c.processing_status == "PROCESSING",
                    messages.c.claim_token == claim_token,
                    messages.c.lease_expires_at > now,
                )
                .values(
                    classification=analysis["classification"],
                    priority_score=analysis["priority_score"],
                    explanation=analysis["explanation"],
                    detected_language=analysis.get("detected_language", "unknown"),
                    requires_response=analysis["requires_response"],
                    suggested_response=analysis["suggested_response"],
                    ai_response_generated_at=self._optional_datetime(
                        analysis.get("ai_response_generated_at")
                    ),
                    processing_status="COMPLETED",
                    processing_error=None,
                    processed_at=now,
                    updated_at=now,
                    lease_expires_at=None,
                    claim_token=None,
                )
            )
            if result.rowcount != 1:
                return None
            row = self._locked_message(
                connection, user_id, conversation_key, message_id
            )
            return self._public_record(_as_dict(row))

    def fail(
        self,
        user_id: str,
        message_id: str,
        claim_token: str,
        safe_error: str,
        retryable: bool,
        retry_after_seconds: int | None,
        retry_max_attempts: int,
        retry_base_delay_seconds: float,
        retry_max_delay_seconds: float,
        conversation_id: str | None = None,
    ) -> int | None:
        now = utc_now()
        conversation_key = self._conversation_key(conversation_id, message_id)
        with self.database.write() as connection:
            row = connection.execute(
                sa.select(messages.c.attempt_count)
                .where(
                    messages.c.user_id == user_id,
                    messages.c.conversation_key == conversation_key,
                    messages.c.message_id == message_id,
                    messages.c.processing_status == "PROCESSING",
                    messages.c.claim_token == claim_token,
                    messages.c.lease_expires_at > now,
                )
                .with_for_update()
            ).first()
            if row is None:
                return None
            attempt_count = int(row.attempt_count or 0)
            can_retry = retryable and attempt_count < retry_max_attempts
            next_attempt_at = None
            cooldown = 0.0
            if can_retry:
                exponential = min(
                    retry_max_delay_seconds,
                    retry_base_delay_seconds * (2 ** max(0, attempt_count - 1)),
                )
                cooldown = max(exponential, min(retry_after_seconds or 0, 86400))
                next_attempt_at = now + timedelta(seconds=cooldown)
            result = connection.execute(
                sa.update(messages)
                .where(
                    messages.c.user_id == user_id,
                    messages.c.conversation_key == conversation_key,
                    messages.c.message_id == message_id,
                    messages.c.processing_status == "PROCESSING",
                    messages.c.claim_token == claim_token,
                    messages.c.lease_expires_at > now,
                )
                .values(
                    processing_status="FAILED",
                    processing_error=safe_error[:250],
                    updated_at=now,
                    lease_expires_at=None,
                    claim_token=None,
                    retryable=can_retry,
                    next_attempt_at=next_attempt_at,
                )
            )
            if result.rowcount != 1:
                return None
            return math.ceil(cooldown) if can_retry else 0

    def renew_lease(
        self,
        user_id: str,
        message_id: str,
        claim_token: str,
        lease_seconds: float,
        conversation_id: str | None = None,
    ) -> bool:
        now = utc_now()
        conversation_key = self._conversation_key(conversation_id, message_id)
        with self.database.write() as connection:
            result = connection.execute(
                sa.update(messages)
                .where(
                    messages.c.user_id == user_id,
                    messages.c.conversation_key == conversation_key,
                    messages.c.message_id == message_id,
                    messages.c.processing_status == "PROCESSING",
                    messages.c.claim_token == claim_token,
                    messages.c.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    updated_at=now,
                )
            )
            return result.rowcount == 1

    def consume_rate_limit(
        self,
        user_id: str,
        limit: int,
        window_seconds: int,
        cost: int = 1,
    ) -> int | None:
        current_epoch = int(time.time())
        window_start = (current_epoch // window_seconds) * window_seconds
        retry_after = max(1, window_start + window_seconds - current_epoch)
        if cost < 1 or cost > limit:
            return retry_after

        with self.database.write() as connection:
            connection.execute(
                sa.delete(rate_limit_buckets).where(
                    rate_limit_buckets.c.window_start < window_start - 2 * window_seconds
                )
            )
            current_count = self._upsert_rate_limit(
                connection, user_id, window_start, limit, cost
            )
            return None if current_count is not None else retry_after

    def check_ready(self) -> None:
        with self.database.read() as connection:
            connection.execute(sa.select(sa.literal(1)).select_from(messages).limit(1)).first()

    def purge_expired_messages(self, retention_days: int, now: datetime | None = None) -> int:
        current = now or utc_now()
        cutoff = current - timedelta(days=retention_days)
        with self.database.write() as connection:
            result = connection.execute(
                sa.delete(messages).where(
                    messages.c.updated_at < cutoff,
                    sa.or_(
                        messages.c.processing_status != "PROCESSING",
                        messages.c.lease_expires_at.is_(None),
                        messages.c.lease_expires_at <= current,
                    ),
                )
            )
            return result.rowcount or 0

    def get(
        self, user_id: str, message_id: str, conversation_id: str | None = None
    ) -> dict[str, Any] | None:
        conversation_key = self._conversation_key(conversation_id, message_id)
        with self.database.read() as connection:
            row = connection.execute(
                sa.select(messages).where(
                    messages.c.user_id == user_id,
                    messages.c.conversation_key == conversation_key,
                    messages.c.message_id == message_id,
                )
            ).mappings().first()
            record = _as_dict(row)
            return self._public_record(record) if record is not None else None

    def get_for_retry(
        self, user_id: str, message_id: str, conversation_id: str | None = None
    ) -> ProcessMessageRequest | None:
        conversation_key = self._conversation_key(conversation_id, message_id)
        with self.database.read() as connection:
            row = connection.execute(
                sa.select(messages).where(
                    messages.c.user_id == user_id,
                    messages.c.conversation_key == conversation_key,
                    messages.c.message_id == message_id,
                    messages.c.processing_status == "FAILED",
                )
            ).mappings().first()
            record = _as_dict(row)
        if record is None:
            return None
        return ProcessMessageRequest(
            message_id=record["message_id"],
            conversation_id=record["conversation_id"],
            sender_name=record["sender_name"],
            original_message=record["original_message"],
            received_at=(
                record["received_at"]
                if record["received_at"] is not None
                and (
                    record["received_at"].tzinfo is not None
                    or record.get("backend_received_at") is not None
                )
                else None
            ),
            conversation_context=(
                [] if record["conversation_id"] is not None else record["conversation_context"]
            ),
            response_language=record["response_language"],
        )

    def list(self, user_id: str, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        with self.database.read() as connection:
            total = connection.execute(
                sa.select(sa.func.count())
                .select_from(messages)
                .where(messages.c.user_id == user_id)
            ).scalar_one()
            rows = connection.execute(
                sa.select(messages)
                .where(messages.c.user_id == user_id)
                .order_by(messages.c.created_at.desc())
                .limit(limit)
                .offset(offset)
            ).mappings().all()
        return [self._public_record(_as_dict(row)) for row in rows], total

    def get_claim_context(
        self, user_id: str, message_id: str, conversation_id: str | None = None
    ) -> list[ContextMessage]:
        conversation_key = self._conversation_key(conversation_id, message_id)
        with self.database.read() as connection:
            row = connection.execute(
                sa.select(messages.c.conversation_context_json).where(
                    messages.c.user_id == user_id,
                    messages.c.conversation_key == conversation_key,
                    messages.c.message_id == message_id,
                )
            ).first()
        if row is None:
            raise RuntimeError("Claimed message context could not be read")
        return [
            ContextMessage.model_validate(item)
            for item in json.loads(row.conversation_context_json or "[]")
        ]

    @staticmethod
    def _content_hash(request: ProcessMessageRequest) -> str:
        identity = json.dumps(
            {
                "sender_name": request.sender_name,
                "original_message": request.original_message,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _conversation_key(conversation_id: str | None, message_id: str) -> str:
        if conversation_id is None:
            identity = f"legacy:{message_id}"
        else:
            identity = f"conversation:{conversation_id}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _conversation_context(
        connection: Connection,
        user_id: str,
        conversation_key: str,
        request: ProcessMessageRequest,
        limit: int,
    ) -> list[ContextMessage]:
        if request.conversation_id is None:
            return request.conversation_context
        if limit == 0:
            return []
        rows = connection.execute(
            sa.select(messages.c.sender_name, messages.c.original_message)
            .where(
                messages.c.user_id == user_id,
                messages.c.conversation_key == conversation_key,
                messages.c.processing_status == "COMPLETED",
                messages.c.message_id != request.message_id,
            )
            .order_by(messages.c.created_at.desc(), messages.c.message_id.desc())
            .limit(limit)
        ).all()
        return [
            ContextMessage(
                sender_name=row.sender_name,
                text=row.original_message[:4000],
            )
            for row in reversed(rows)
        ]

    @staticmethod
    def _optional_datetime(value: datetime | str | None) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Persisted processing timestamps must be timezone-aware")
        return parsed.astimezone(UTC)

    @staticmethod
    def _lease_is_active(lease_expires_at: datetime | str | None, now: datetime) -> bool:
        if lease_expires_at is None:
            return False
        try:
            expiry = MessageRepository._as_datetime(lease_expires_at)
        except (TypeError, ValueError):
            return False
        return expiry > MessageRepository._as_datetime(now)

    @staticmethod
    def _retry_after_seconds(next_attempt_at: datetime | str, now: datetime) -> int:
        try:
            retry_at = MessageRepository._as_datetime(next_attempt_at)
            current = MessageRepository._as_datetime(now)
        except (TypeError, ValueError):
            return 0
        return max(0, math.ceil((retry_at - current).total_seconds()))

    @staticmethod
    def _as_datetime(value: datetime | str) -> datetime:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    @staticmethod
    def _public_record(record: dict[str, Any] | None) -> dict[str, Any]:
        if record is None:
            raise ValueError("Expected a persisted message record")
        result = dict(record)
        backend_timestamp_fields = (
            "backend_received_at",
            "processing_started_at",
            "lease_expires_at",
            "next_attempt_at",
            "created_at",
            "updated_at",
            "processed_at",
            "ai_response_generated_at",
        )
        for field in backend_timestamp_fields:
            if result.get(field) is not None:
                result[field] = MessageRepository._as_datetime(result[field]).astimezone(UTC)
        if result.get("processing_started_at") and result.get("processed_at"):
            started_at = MessageRepository._as_datetime(result["processing_started_at"])
            processed_at = MessageRepository._as_datetime(result["processed_at"])
            duration = (processed_at - started_at).total_seconds()
            result["processing_duration_ms"] = (
                int(duration * 1000) if duration >= 0 else None
            )
        else:
            result["processing_duration_ms"] = None
        for field in (
            "user_id",
            "conversation_key",
            "request_hash",
            "conversation_context",
            "conversation_context_json",
            "response_language",
            "claim_token",
            "lease_expires_at",
            "attempt_count",
            "retryable",
            "next_attempt_at",
        ):
            result.pop(field, None)
        if result.get("requires_response") is not None:
            result["requires_response"] = bool(result["requires_response"])
        return result

    @staticmethod
    def _locked_message(
        connection: Connection,
        user_id: str,
        conversation_key: str,
        message_id: str,
    ):
        statement = sa.select(messages).where(
            messages.c.user_id == user_id,
            messages.c.conversation_key == conversation_key,
            messages.c.message_id == message_id,
        )
        if connection.dialect.name == "postgresql":
            statement = statement.with_for_update()
        return connection.execute(statement).mappings().first()

    @staticmethod
    def _insert_ignore_conflict(
        connection: Connection,
        table: sa.Table,
        values: dict[str, Any],
        index_elements: tuple[sa.Column, ...],
    ):
        if connection.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif connection.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            raise RuntimeError("Unsupported database dialect")
        return insert(table).values(**values).on_conflict_do_nothing(
            index_elements=list(index_elements)
        )

    @staticmethod
    def _upsert_rate_limit(
        connection: Connection,
        user_id: str,
        window_start: int,
        limit: int,
        cost: int,
    ) -> int | None:
        if connection.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif connection.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            raise RuntimeError("Unsupported database dialect")
        statement = insert(rate_limit_buckets).values(
            user_id=user_id,
            window_start=window_start,
            request_count=cost,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[rate_limit_buckets.c.user_id, rate_limit_buckets.c.window_start],
            set_={
                "request_count": rate_limit_buckets.c.request_count + cost,
            },
            where=rate_limit_buckets.c.request_count + cost <= limit,
        ).returning(rate_limit_buckets.c.request_count)
        row = connection.execute(statement).first()
        return int(row[0]) if row else None
