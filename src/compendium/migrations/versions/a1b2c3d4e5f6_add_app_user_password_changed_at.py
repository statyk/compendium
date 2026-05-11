"""add_app_user_password_changed_at

Revision ID: a1b2c3d4e5f6
Revises: fab1c2d3e4f5
Create Date: 2026-05-11 00:00:00.000000

Adds ``password_changed_at`` (nullable TIMESTAMP) to ``app_user``.
Backfills existing rows to the migration time so JWTs issued after upgrade
will have a valid ``pwd_iat`` claim to compare against.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '611abe9ea6e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'app_user',
        sa.Column('password_changed_at', sa.DateTime(), nullable=True),
    )
    op.execute("UPDATE app_user SET password_changed_at = CURRENT_TIMESTAMP WHERE password_changed_at IS NULL")


def downgrade() -> None:
    op.drop_column('app_user', 'password_changed_at')
