"""add_hold_held_item

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-04-23 09:00:00.000000

Adds ``hold.held_item_id`` so that AVAILABLE holds (and, once the matching
service-layer changes land, immediately-promoted holds on place) can record
which specific copy has been set aside for pickup.

No data backfill: existing WAITING-with-available-copy holds stay in that
(now-repairable) state and can be nudged with an ad-hoc SQL promotion
script against a test instance if desired.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('hold') as batch:
        batch.add_column(sa.Column('held_item_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_hold_held_item_id',
            'item',
            ['held_item_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch.create_index('ix_hold_held_item_id', ['held_item_id'])


def downgrade() -> None:
    with op.batch_alter_table('hold') as batch:
        batch.drop_index('ix_hold_held_item_id')
        batch.drop_constraint('fk_hold_held_item_id', type_='foreignkey')
        batch.drop_column('held_item_id')
