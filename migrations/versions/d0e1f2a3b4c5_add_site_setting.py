"""add_site_setting

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-04-24 11:00:00.000000

Adds the ``site_setting`` table — the first step of the env-backed-to-DB
settings migration (see CLAUDE.md site-settings design note).

Table is small (<100 rows), key/value text pairs with an updated_at column
that doubles as a cache-invalidation epoch source. value is JSON-encoded
text so type info round-trips across SQLite + Postgres without needing
backend-specific JSON column ops.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'site_setting',
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.Column('updated_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ['updated_by_id'], ['app_user.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('site_setting')
