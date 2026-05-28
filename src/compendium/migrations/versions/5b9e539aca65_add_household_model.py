"""add_household_model

Revision ID: 5b9e539aca65
Revises: c4d5e6f7a8b9
Create Date: 2026-05-28 16:09:00.138291

Adds Household table and household_id FK on patron.
Adds household.manage permission to Librarian preset role.
"""
from typing import Sequence, Union
import json

import sqlalchemy as sa
from alembic import op

revision: str = '5b9e539aca65'
down_revision: Union[str, Sequence[str], None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_PERM = "household.manage"


def upgrade() -> None:
    op.create_table(
        "household",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    with op.batch_alter_table("patron", schema=None) as batch_op:
        batch_op.add_column(sa.Column("household_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_patron_household_id",
            "household",
            ["household_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_patron_household_id", ["household_id"])

    # Add household.manage to the Librarian preset role
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

    with op.batch_alter_table("patron", schema=None) as batch_op:
        batch_op.drop_index("ix_patron_household_id")
        batch_op.drop_constraint("fk_patron_household_id", type_="foreignkey")
        batch_op.drop_column("household_id")

    op.drop_table("household")
