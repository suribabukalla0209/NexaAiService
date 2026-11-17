"""Add isolated conversation identity and backend processing timestamps.

Revision ID: 0003_conversations_and_timestamps
Revises: 0002_detected_language
"""

import hashlib

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0003_conversations_and_timestamps"
down_revision = "0002_detected_language"
branch_labels = None
depends_on = None

_TABLE = "whatsapp_ai_messages"
_OLD_PRIMARY_KEY = ["user_id", "message_id"]
_NEW_PRIMARY_KEY = ["user_id", "conversation_key", "message_id"]
_PK_NAME = "pk_whatsapp_ai_messages"
_HISTORY_INDEX = "ix_whatsapp_ai_messages_conversation_history"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        raise RuntimeError("Cannot add conversation identity before the message table exists")

    existing_columns = {
        column["name"] for column in inspector.get_columns(_TABLE)
    }
    additions = (
        sa.Column("conversation_id", sa.String(200), nullable=True),
        sa.Column("conversation_key", sa.String(420), nullable=True),
        sa.Column("backend_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ai_response_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in additions:
        if column.name not in existing_columns:
            op.add_column(_TABLE, column)

    legacy_rows = bind.execute(
        sa.text(
            "SELECT user_id, message_id FROM whatsapp_ai_messages "
            "WHERE conversation_key IS NULL"
        )
    ).mappings().all()
    for row in legacy_rows:
        key = hashlib.sha256(
            f"legacy:{row['message_id']}".encode("utf-8")
        ).hexdigest()
        bind.execute(
            sa.text(
                "UPDATE whatsapp_ai_messages SET conversation_key = :key "
                "WHERE user_id = :user_id AND message_id = :message_id "
                "AND conversation_key IS NULL"
            ),
            {
                "key": key,
                "user_id": row["user_id"],
                "message_id": row["message_id"],
            },
        )

    inspector = inspect(bind)
    primary_key = inspector.get_pk_constraint(_TABLE)
    primary_key_columns = primary_key.get("constrained_columns") or []
    key_column = next(
        column
        for column in inspector.get_columns(_TABLE)
        if column["name"] == "conversation_key"
    )
    if primary_key_columns not in (_OLD_PRIMARY_KEY, _NEW_PRIMARY_KEY, []):
        raise RuntimeError(
            "Unsupported whatsapp_ai_messages primary key; refusing identity migration"
        )

    if bind.dialect.name == "sqlite":
        if primary_key_columns != _NEW_PRIMARY_KEY or key_column["nullable"]:
            with op.batch_alter_table(
                _TABLE,
                recreate="always",
                naming_convention={"pk": _PK_NAME},
            ) as batch:
                if primary_key_columns:
                    batch.drop_constraint(
                        primary_key.get("name") or _PK_NAME,
                        type_="primary",
                    )
                if key_column["nullable"]:
                    batch.alter_column(
                        "conversation_key",
                        existing_type=sa.String(420),
                        nullable=False,
                    )
                if primary_key_columns != _NEW_PRIMARY_KEY:
                    batch.create_primary_key(_PK_NAME, _NEW_PRIMARY_KEY)
    elif bind.dialect.name == "postgresql":
        if key_column["nullable"]:
            op.alter_column(
                _TABLE,
                "conversation_key",
                existing_type=sa.String(420),
                nullable=False,
            )
        if primary_key_columns != _NEW_PRIMARY_KEY:
            if primary_key_columns:
                name = primary_key.get("name")
                if not name:
                    raise RuntimeError(
                        "Cannot safely replace an unnamed message primary key"
                    )
                op.drop_constraint(name, _TABLE, type_="primary")
            op.create_primary_key(_PK_NAME, _TABLE, _NEW_PRIMARY_KEY)
    else:
        raise RuntimeError("Unsupported database dialect for conversation identity migration")

    if _HISTORY_INDEX not in {
        index["name"] for index in inspect(bind).get_indexes(_TABLE)
    }:
        op.create_index(
            _HISTORY_INDEX,
            _TABLE,
            [
                "user_id",
                "conversation_key",
                "processing_status",
                "created_at",
                "message_id",
            ],
        )


def downgrade() -> None:
    # Keep the new identity and timestamps; dropping them could lose conversation data.
    pass
