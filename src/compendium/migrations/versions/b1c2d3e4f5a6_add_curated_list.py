"""add_curated_list

Revision ID: b1c2d3e4f5a6
Revises: 443891bfaa50
Create Date: 2026-05-30 12:00:00.000000

Adds curated_list and curated_list_entry tables for librarian-curated
collections (staff picks, summer reads, etc.).
Adds curatedlist.manage permission to Librarian preset role.
"""
from typing import Sequence, Union
import json

import sqlalchemy as sa
from alembic import op

revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = '443891bfaa50'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_PERM = "curatedlist.manage"


def upgrade() -> None:
    op.create_table(
        "curated_list",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("slug", sa.String(length=96), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "is_featured",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "display_order",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("slug", name="uq_curated_list_slug"),
    )

    op.create_table(
        "curated_list_entry",
        sa.Column("list_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column(
            "display_order",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("annotation", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("list_id", "work_id", name="pk_curated_list_entry"),
        sa.ForeignKeyConstraint(
            ["list_id"],
            ["curated_list.id"],
            name="fk_curated_list_entry_list_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["work.id"],
            name="fk_curated_list_entry_work_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_curated_list_entry_list_id", "curated_list_entry", ["list_id"])

    # Add curatedlist.manage to the Librarian preset role
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


def downgrade() -> None:
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

    op.drop_index("ix_curated_list_entry_list_id", table_name="curated_list_entry")
    op.drop_table("curated_list_entry")
    op.drop_table("curated_list")
