"""Add encrypted provider control-plane storage and provider-qualified IDs.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "provider_configs" not in tables:
        op.create_table(
            "provider_configs",
            sa.Column("provider", sa.String(length=32), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column("encrypted_secrets", sa.Text(), nullable=False, server_default=""),
            sa.Column("nonce", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("updated_by", sa.String(length=100), nullable=False, server_default="system"),
            sa.Column("last_check_status", sa.String(length=32), nullable=True),
            sa.Column("last_check_details", sa.JSON(), nullable=False),
            sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_provider_configs_enabled", "provider_configs", ["enabled"])

    message_columns = {column["name"] for column in inspect(bind).get_columns("messages")}
    if "provider" not in message_columns:
        op.add_column("messages", sa.Column("provider", sa.String(length=32), nullable=True))
        op.create_index("ix_messages_provider", "messages", ["provider"])

    message_constraints = inspect(bind).get_unique_constraints("messages")
    composite_exists = False
    for constraint in message_constraints:
        columns = tuple(constraint.get("column_names") or ())
        if set(columns) == {"provider", "provider_message_id"}:
            composite_exists = True
        elif columns == ("provider_message_id",) and constraint.get("name"):
            op.drop_constraint(constraint["name"], "messages", type_="unique")
    if not composite_exists:
        op.create_unique_constraint(
            "uq_message_provider_id",
            "messages",
            ["provider", "provider_message_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "provider_configs" in inspector.get_table_names():
        op.drop_index("ix_provider_configs_enabled", table_name="provider_configs")
        op.drop_table("provider_configs")
    message_columns = {column["name"] for column in inspect(bind).get_columns("messages")}
    if "provider" in message_columns:
        constraints = inspect(bind).get_unique_constraints("messages")
        for constraint in constraints:
            if set(constraint.get("column_names") or ()) == {
                "provider",
                "provider_message_id",
            } and constraint.get("name"):
                op.drop_constraint(constraint["name"], "messages", type_="unique")
                break
        op.drop_index("ix_messages_provider", table_name="messages")
        op.drop_column("messages", "provider")
