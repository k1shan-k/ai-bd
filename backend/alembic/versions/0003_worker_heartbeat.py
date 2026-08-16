"""Add durable worker health heartbeat.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "worker_heartbeats" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "worker_heartbeats",
            sa.Column("name", sa.String(length=64), primary_key=True),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("details", sa.JSON(), nullable=False),
        )


def downgrade() -> None:
    if "worker_heartbeats" in inspect(op.get_bind()).get_table_names():
        op.drop_table("worker_heartbeats")
