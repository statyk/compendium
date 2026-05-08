"""fix_failed_login_id_type

Revision ID: e9f0a1b2c3d4
Revises: d7e8f9a0b1c2
Create Date: 2026-05-08 00:00:00.000000

The original add_failed_login_table migration declared id as BigInteger.
On SQLite, BIGINT PRIMARY KEY is not a rowid alias and does not autoincrement,
so INSERTs without an explicit id fail with NOT NULL. The ORM model already
uses plain Mapped[int] (= Integer / INTEGER), matching what create_all produces
in tests. Drop and rebuild with Integer so existing installs match the model.

The table is a sliding-window failure log; losing in-flight rows is acceptable.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: Union[str, Sequence[str], None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_failed_login_scope_id_at", table_name="failed_login")
    op.drop_table("failed_login")
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
    op.create_table(
        "failed_login",
        sa.Column("id", sa.BigInteger(), nullable=False),
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
