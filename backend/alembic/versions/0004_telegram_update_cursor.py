"""Add durable per-account Telegram update cursors.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "telegram_update_cursors" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "telegram_update_cursors",
            sa.Column("provider_account_id", sa.String(length=64), primary_key=True),
            sa.Column("chat_id", sa.String(length=64), primary_key=True),
            sa.Column("message_id", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    if "telegram_update_cursors" in inspect(op.get_bind()).get_table_names():
        op.drop_table("telegram_update_cursors")
