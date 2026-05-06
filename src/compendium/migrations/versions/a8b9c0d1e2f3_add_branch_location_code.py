"""add_branch_location_code

Revision ID: a8b9c0d1e2f3
Revises: f2a3b4c5d6e7
Create Date: 2026-05-03 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a8b9c0d1e2f3'
down_revision: Union[str, Sequence[str], None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('branch', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('location_code', sa.String(4), nullable=True)
        )
        batch_op.create_unique_constraint('uq_branch_location_code', ['location_code'])


def downgrade() -> None:
    with op.batch_alter_table('branch', schema=None) as batch_op:
        batch_op.drop_constraint('uq_branch_location_code', type_='unique')
        batch_op.drop_column('location_code')
