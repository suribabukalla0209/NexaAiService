"""Create portable WhatsApp message and rate-limit schema.

Revision ID: 0001_portable_database
Revises:
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.database.schema import metadata, messages, rate_limit_buckets

revision = "0001_portable_database"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "whatsapp_ai_messages" not in existing_tables:
        metadata.create_all(bind=bind, tables=[messages, rate_limit_buckets], checkfirst=True)
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("whatsapp_ai_messages")
    }
    retryable_default = sa.text("1" if bind.dialect.name == "sqlite" else "true")
    additions = (
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=retryable_default),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in additions:
        if column.name not in existing_columns:
            op.add_column("whatsapp_ai_messages", column)

    metadata.create_all(bind=bind, tables=[rate_limit_buckets], checkfirst=True)


def downgrade() -> None:
    # Deliberately non-destructive: downgrade never deletes user message data.
    pass
