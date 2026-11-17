"""Persist AI-detected source message language.

Revision ID: 0002_detected_language
Revises: 0001_portable_database
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0002_detected_language"
down_revision = "0001_portable_database"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in inspect(op.get_bind()).get_columns("whatsapp_ai_messages")
    }
    if "detected_language" not in columns:
        op.add_column(
            "whatsapp_ai_messages",
            sa.Column("detected_language", sa.String(length=80), nullable=True),
        )


def downgrade() -> None:
    # Deliberately non-destructive; existing AI and notification records are retained.
    pass
