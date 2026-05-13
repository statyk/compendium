"""add_metadata_cache

Revision ID: 611abe9ea6e5
Revises: fab1c2d3e4f5
Create Date: 2026-05-09 15:27:17.498719

Persistent cache for external metadata lookups (Google Books, Open Library, MusicBrainz, TMDb).
Keyed on (adapter, kind, lookup_value); payload is JSON-encoded; NULL on negative rows.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '611abe9ea6e5'
down_revision: Union[str, Sequence[str], None] = 'fab1c2d3e4f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'metadata_cache',
        sa.Column('adapter',       sa.String(64),  nullable=False),
        sa.Column('kind',          sa.String(32),  nullable=False),
        sa.Column('lookup_value',  sa.String(255), nullable=False),
        sa.Column('payload',       sa.Text(),      nullable=True),
        sa.Column('is_negative',   sa.Boolean(),   nullable=False, server_default='0'),
        sa.Column('fetched_at',    sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('adapter', 'kind', 'lookup_value'),
    )
    with op.batch_alter_table('metadata_cache') as batch_op:
        batch_op.create_index('ix_metadata_cache_fetched_at', ['fetched_at'])


def downgrade() -> None:
    with op.batch_alter_table('metadata_cache') as batch_op:
        batch_op.drop_index('ix_metadata_cache_fetched_at')
    op.drop_table('metadata_cache')
