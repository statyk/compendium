"""add_library_calendar

Revision ID: c4d5e6f7a8b9
Revises: 1b17e2ba445c
Create Date: 2026-05-28 00:00:00.000000

Adds library_hours (weekly schedule) and closed_date tables.
Seeds seven library_hours rows (Mon–Sun, all open 00:00–23:59) so that
existing due-date behaviour is preserved before a librarian configures hours.
Also adds the calendar.manage permission to the Librarian preset role.
"""
from datetime import time
from typing import Sequence, Union
import json

import sqlalchemy as sa
from alembic import op

revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = '1b17e2ba445c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_PERM = "calendar.manage"

_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def upgrade() -> None:
    op.create_table(
        "library_hours",
        sa.Column("weekday", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("open_time", sa.Time(), nullable=True),
        sa.Column("close_time", sa.Time(), nullable=True),
    )

    op.create_table(
        "closed_date",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("label", sa.String(128), nullable=True),
        sa.Column("recurs_annually", sa.Boolean(), nullable=False, server_default="0"),
    )

    op.create_index("ix_closed_date_start", "closed_date", ["start_date"])

    # Seed all seven weekdays as open with default 00:00–23:59 hours
    bind = op.get_bind()
    hours_t = sa.Table(
        "library_hours",
        sa.MetaData(),
        sa.Column("weekday", sa.Integer()),
        sa.Column("is_open", sa.Boolean()),
        sa.Column("open_time", sa.Time()),
        sa.Column("close_time", sa.Time()),
    )
    bind.execute(
        hours_t.insert(),
        [
            {"weekday": wd, "is_open": True, "open_time": time(0, 0), "close_time": time(23, 59)}
            for wd in range(7)
        ],
    )

    # Add calendar.manage to the Librarian preset role
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
    # Remove calendar.manage from Librarian
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

    op.drop_index("ix_closed_date_start", table_name="closed_date")
    op.drop_table("closed_date")
    op.drop_table("library_hours")
