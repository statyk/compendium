"""patron_account_manage

Revision ID: fab1c2d3e4f5
Revises: f6a7b8c9d0e1
Create Date: 2026-05-09 00:00:00.000000

Adds the ``patron.account.manage`` permission to the Librarian preset and a
partial unique index on ``patron.user_id`` (enforces the 1:1 service-layer
invariant at the DB level).
"""
from typing import Sequence, Union
import json

import sqlalchemy as sa
from alembic import op

revision: str = 'fab1c2d3e4f5'
down_revision: Union[str, Sequence[str], None] = 'e9f0a1b2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_PERM = "patron.account.manage"


def upgrade() -> None:
    bind = op.get_bind()
    role_t = sa.Table(
        "role",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64)),
        sa.Column("permissions", sa.JSON),
    )

    rows = list(bind.execute(sa.select(role_t).where(role_t.c.name == "Librarian")))
    if rows:
        lib = rows[0]
        perms = lib.permissions
        if isinstance(perms, str):
            try:
                perms = json.loads(perms)
            except json.JSONDecodeError:
                perms = []
        if isinstance(perms, list) and _NEW_PERM not in perms:
            bind.execute(
                role_t.update()
                .where(role_t.c.id == lib.id)
                .values(permissions=perms + [_NEW_PERM])
            )

    op.create_index(
        "ix_patron_user_id_unique",
        "patron",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
        sqlite_where=sa.text("user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_patron_user_id_unique", table_name="patron")

    bind = op.get_bind()
    role_t = sa.Table(
        "role",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64)),
        sa.Column("permissions", sa.JSON),
    )
    rows = list(bind.execute(sa.select(role_t).where(role_t.c.name == "Librarian")))
    if rows:
        lib = rows[0]
        perms = lib.permissions
        if isinstance(perms, str):
            try:
                perms = json.loads(perms)
            except json.JSONDecodeError:
                perms = []
        if isinstance(perms, list) and _NEW_PERM in perms:
            bind.execute(
                role_t.update()
                .where(role_t.c.id == lib.id)
                .values(permissions=[p for p in perms if p != _NEW_PERM])
            )
