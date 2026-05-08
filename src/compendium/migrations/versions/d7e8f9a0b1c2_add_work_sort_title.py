"""add_work_sort_title

Revision ID: d7e8f9a0b1c2
Revises: c3d4e5f6a7b8
Create Date: 2026-05-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'work',
        sa.Column('sort_title', sa.String(512), nullable=False, server_default=''),
    )
    op.create_index('ix_work_sort_title', 'work', ['sort_title'])

    from compendium.services._normalization import compute_sort_title
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, title FROM work")).fetchall()
    for row in rows:
        bind.execute(
            sa.text("UPDATE work SET sort_title = :st WHERE id = :id"),
            {"st": compute_sort_title(row.title), "id": row.id},
        )


def downgrade() -> None:
    op.drop_index('ix_work_sort_title', table_name='work')
    op.drop_column('work', 'sort_title')
