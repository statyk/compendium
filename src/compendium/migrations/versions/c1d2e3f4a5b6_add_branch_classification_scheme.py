"""add_branch_classification_scheme

Revision ID: c1d2e3f4a5b6
Revises: 11dbd4cede12
Create Date: 2026-04-19 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = '11dbd4cede12'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('branch', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'default_classification_scheme',
                sa.String(8),
                nullable=False,
                server_default='none',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('branch', schema=None) as batch_op:
        batch_op.drop_column('default_classification_scheme')
