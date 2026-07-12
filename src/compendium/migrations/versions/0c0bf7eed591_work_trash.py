"""work_trash

Revision ID: 0c0bf7eed591
Revises: d3e4f5a6b7c8
Create Date: 2026-07-11 00:00:00.000000

Adds the ``deleted_entity`` trash table (snapshot-based recoverable work
deletion) and the ``work.delete`` permission to the Librarian preset.
"""
from typing import Sequence, Union
import json

import sqlalchemy as sa
from alembic import op

revision: str = '0c0bf7eed591'
down_revision: Union[str, Sequence[str], None] = 'd3e4f5a6b7c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_PERM = "work.delete"

_role_t = sa.Table(
    "role",
    sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(64)),
    sa.Column("permissions", sa.JSON),
)


def upgrade() -> None:
    op.create_table(
        "deleted_entity",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.Integer, nullable=False),
        sa.Column("label", sa.String(512), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "deleted_by", sa.Integer, sa.ForeignKey("app_user.id"), nullable=True
        ),
    )
    op.create_index(
        "ix_deleted_entity_type_deleted_at",
        "deleted_entity",
        ["entity_type", "deleted_at"],
    )

    bind = op.get_bind()
    rows = list(bind.execute(sa.select(_role_t).where(_role_t.c.name == "Librarian")))
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
                _role_t.update()
                .where(_role_t.c.id == lib.id)
                .values(permissions=perms + [_NEW_PERM])
            )


def downgrade() -> None:
    bind = op.get_bind()
    rows = list(bind.execute(sa.select(_role_t).where(_role_t.c.name == "Librarian")))
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
                _role_t.update()
                .where(_role_t.c.id == lib.id)
                .values(permissions=[p for p in perms if p != _NEW_PERM])
            )
    op.drop_index("ix_deleted_entity_type_deleted_at", table_name="deleted_entity")
    op.drop_table("deleted_entity")
