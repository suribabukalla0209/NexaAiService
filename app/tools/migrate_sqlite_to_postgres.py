from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import Settings
from app.database.engine import Database
from app.database.schema import messages, rate_limit_buckets


def _datetime_value(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        # Legacy SQLite DATETIME values do not retain offsets; the migration
        # policy is to interpret those historical values as UTC.
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _normalize_message(row: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    normalized = {column.name: row.get(column.name) for column in messages.columns}
    normalized["conversation_id"] = normalized["conversation_id"] or None
    normalized["conversation_key"] = normalized["conversation_key"] or hashlib.sha256(
        f"legacy:{normalized['message_id']}".encode("utf-8")
    ).hexdigest()
    normalized["request_hash"] = normalized["request_hash"] or hashlib.sha256(
        json.dumps(
            {
                "sender_name": normalized["sender_name"],
                "original_message": normalized["original_message"],
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    normalized["conversation_context_json"] = normalized["conversation_context_json"] or "[]"
    normalized["response_language"] = normalized["response_language"] or "same as message"
    normalized["processing_status"] = normalized["processing_status"] or "PENDING"
    normalized["suggested_response"] = normalized["suggested_response"] or ""
    normalized["attempt_count"] = int(normalized["attempt_count"] or 0)
    normalized["retryable"] = bool(normalized["retryable"] if normalized["retryable"] is not None else True)
    for key in (
        "received_at",
        "backend_received_at",
        "processing_started_at",
        "lease_expires_at",
        "next_attempt_at",
        "created_at",
        "updated_at",
        "processed_at",
        "ai_response_generated_at",
    ):
        normalized[key] = _datetime_value(normalized[key])
    normalized["created_at"] = normalized["created_at"] or now
    normalized["updated_at"] = normalized["updated_at"] or normalized["created_at"]
    if normalized["requires_response"] is not None:
        normalized["requires_response"] = bool(normalized["requires_response"])
    return normalized


def _chunks(rows: Iterable[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _message_digest(records: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        canonical = {}
        for key in messages.columns.keys():
            value = record[key]
            if isinstance(value, datetime):
                value = _datetime_value(value).isoformat()
            canonical[key] = value
        digest.update(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _read_source(source_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not source_path.is_file():
        raise FileNotFoundError("SQLite source file does not exist")
    source_engine = sa.create_engine(
        f"sqlite:///{source_path.as_posix()}",
        connect_args={"timeout": 10},
    )
    try:
        inspector = sa.inspect(source_engine)
        if "whatsapp_ai_messages" not in inspector.get_table_names():
            raise RuntimeError("SQLite source has no WhatsApp AI message table")
        source_messages = sa.Table(
            "whatsapp_ai_messages", sa.MetaData(), autoload_with=source_engine
        )
        with source_engine.connect() as connection:
            raw_rows = connection.execute(sa.select(source_messages)).mappings().all()
        message_rows = [_normalize_message(dict(row)) for row in raw_rows]

        rate_rows: list[dict[str, Any]] = []
        if "api_rate_limit_buckets" in inspector.get_table_names():
            source_rates = sa.Table(
                "api_rate_limit_buckets", sa.MetaData(), autoload_with=source_engine
            )
            with source_engine.connect() as connection:
                rate_rows = [dict(row) for row in connection.execute(sa.select(source_rates)).mappings()]
        return message_rows, rate_rows
    finally:
        source_engine.dispose()


def _count_destination(database: Database, table: sa.Table) -> int:
    inspector = sa.inspect(database.engine)
    if table.name not in inspector.get_table_names():
        return 0
    with database.read() as connection:
        return int(connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())


def _write_destination(
    database: Database,
    message_rows: list[dict[str, Any]],
    rate_rows: list[dict[str, Any]],
    batch_size: int,
) -> None:
    with database.write() as connection:
        for batch in _chunks(message_rows, batch_size):
            connection.execute(
                pg_insert(messages)
                .values(batch)
                .on_conflict_do_nothing(
                    index_elements=[
                        messages.c.user_id,
                        messages.c.conversation_key,
                        messages.c.message_id,
                    ]
                )
            )
        for batch in _chunks(rate_rows, batch_size):
            connection.execute(
                pg_insert(rate_limit_buckets)
                .values(batch)
                .on_conflict_do_nothing(
                    index_elements=[rate_limit_buckets.c.user_id, rate_limit_buckets.c.window_start]
                )
            )


def _verify_messages(database: Database, expected: list[dict[str, Any]]) -> str:
    actual_rows: list[dict[str, Any]] = []
    with database.read() as connection:
        for batch in _chunks(expected, 500):
            keys = [
                (row["user_id"], row["conversation_key"], row["message_id"])
                for row in batch
            ]
            statement = sa.select(messages).where(
                sa.tuple_(
                    messages.c.user_id,
                    messages.c.conversation_key,
                    messages.c.message_id,
                ).in_(keys)
            )
            actual_rows.extend(dict(row) for row in connection.execute(statement).mappings())
    actual_rows.sort(
        key=lambda row: (
            row["user_id"],
            row["conversation_key"],
            row["message_id"],
        )
    )
    expected_sorted = sorted(
        expected,
        key=lambda row: (
            row["user_id"],
            row["conversation_key"],
            row["message_id"],
        ),
    )
    if len(actual_rows) != len(expected_sorted):
        raise RuntimeError("PostgreSQL message count validation failed")
    if _message_digest(actual_rows) != _message_digest(expected_sorted):
        raise RuntimeError("PostgreSQL message content validation failed")
    return _message_digest(actual_rows)


def migrate(source_path: Path, destination_url: str, dry_run: bool, batch_size: int = 500) -> dict[str, Any]:
    if not destination_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("Destination must be PostgreSQL")
    source_messages, source_rates = _read_source(source_path)
    destination = Database(destination_url)
    try:
        if not dry_run:
            destination.migrate()
        existing_message_count = _count_destination(destination, messages)
        existing_rate_count = _count_destination(destination, rate_limit_buckets)
        report = {
            "source_messages": len(source_messages),
            "source_rate_limit_buckets": len(source_rates),
            "destination_messages_before": existing_message_count,
            "destination_rate_buckets_before": existing_rate_count,
            "dry_run": dry_run,
        }
        if dry_run:
            return report
        _write_destination(destination, source_messages, source_rates, batch_size)
        migrated_rows: list[dict[str, Any]] = []
        with destination.read() as connection:
            for batch in _chunks(source_messages, 500):
                keys = [
                    (row["user_id"], row["conversation_key"], row["message_id"])
                    for row in batch
                ]
                migrated_rows.extend(
                    dict(row)
                    for row in connection.execute(
                        sa.select(messages).where(
                            sa.tuple_(
                                messages.c.user_id,
                                messages.c.conversation_key,
                                messages.c.message_id,
                            ).in_(keys)
                        )
                    ).mappings()
                )
        digest = _verify_messages(destination, source_messages)
        target_message_count = _count_destination(destination, messages)
        target_rate_count = _count_destination(destination, rate_limit_buckets)
        missing_rates = []
        mismatched_rates = []
        with destination.read() as connection:
            for row in source_rates:
                found = connection.execute(
                    sa.select(rate_limit_buckets).where(
                        rate_limit_buckets.c.user_id == row["user_id"],
                        rate_limit_buckets.c.window_start == row["window_start"],
                    )
                ).mappings().first()
                if found is None:
                    missing_rates.append(row)
                elif int(found["request_count"]) != int(row["request_count"]):
                    mismatched_rates.append(row)
        report.update(
            {
                "destination_messages_after": target_message_count,
                "destination_rate_buckets_after": target_rate_count,
                "validated_source_message_digest": digest,
                "destination_verified_message_digest": _message_digest(
                    sorted(migrated_rows, key=lambda row: (row["user_id"], row["message_id"]))
                ),
            }
        )
        if (
            target_message_count < len(source_messages)
            or target_rate_count < len(source_rates)
            or missing_rates
            or mismatched_rates
        ):
            raise RuntimeError("PostgreSQL record-count validation failed")
        return report
    finally:
        destination.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy SQLite AI message and rate-limit data to PostgreSQL without deleting the source."
    )
    parser.add_argument("--source", required=True, type=Path, help="Path to the SQLite database file")
    parser.add_argument("--dry-run", action="store_true", help="Count source/destination data without writing")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 5000:
        parser.error("--batch-size must be between 1 and 5000")

    settings = Settings()
    destination_url = settings.database_url.get_secret_value() if settings.database_url else ""
    if not destination_url:
        raise RuntimeError("Set NEXA_DATABASE_URL to the PostgreSQL destination before running migration")
    report = migrate(args.source.expanduser().resolve(), destination_url, args.dry_run, args.batch_size)
    for key, value in report.items():
        print(f"{key}={value}")
    print("SQLite source was not modified or deleted.")


if __name__ == "__main__":
    main()
