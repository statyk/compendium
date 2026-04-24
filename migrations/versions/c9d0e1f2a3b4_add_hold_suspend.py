"""add_hold_suspend

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-04-23 17:00:00.000000

Adds ``hold.suspended_until`` (Date, nullable) + ``hold.suspended_reason``
(String(256), nullable). A WAITING hold with ``suspended_until`` set (or NULL
for "indefinite") is skipped by the queue-promotion logic; librarians
resume it manually or a maintenance command resumes holds whose
``suspended_until`` has passed.

No status change: the hold stays WAITING; suspension is orthogonal.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('hold') as batch:
        batch.add_column(sa.Column('suspended_until', sa.Date(), nullable=True))
        batch.add_column(sa.Column('suspended_reason', sa.String(256), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('hold') as batch:
        batch.drop_column('suspended_reason')
        batch.drop_column('suspended_until')
