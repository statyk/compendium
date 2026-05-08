"""add_failed_login_table

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-04-30 00:00:00.000000

Records failed login attempts so the application can enforce a per-identity
sliding-window throttle without a runtime dependency on Redis or slowapi.

Table is intentionally a simple append-only log: no FK to app_user or patron
so that attempts against non-existent usernames are still counted.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "failed_login",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("identifier", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_failed_login_scope_id_at",
        "failed_login",
        ["scope", "identifier", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_failed_login_scope_id_at", table_name="failed_login")
    op.drop_table("failed_login")
