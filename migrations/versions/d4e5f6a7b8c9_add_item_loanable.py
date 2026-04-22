"""add_item_loanable

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-04-21 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('item', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'is_loanable',
                sa.Boolean(),
                nullable=False,
                server_default='1',
            )
        )
        batch_op.add_column(
            sa.Column('loan_restriction_reason', sa.String(32), nullable=True)
        )
        batch_op.add_column(
            sa.Column('loan_restriction_note', sa.String(256), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('item', schema=None) as batch_op:
        batch_op.drop_column('loan_restriction_note')
        batch_op.drop_column('loan_restriction_reason')
        batch_op.drop_column('is_loanable')
