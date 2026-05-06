"""add_counters_table

Revision ID: b2c3d4e5f6a7
Revises: a8b9c0d1e2f3
Create Date: 2026-05-03 12:01:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a8b9c0d1e2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'counters',
        sa.Column('key', sa.String(64), primary_key=True),
        sa.Column('value', sa.BigInteger(), nullable=False),
    )

    conn = op.get_bind()
    # Seed catalog.accession from the current max accession number so that
    # new mints never collide with existing rows.  Filter non-numeric values
    # in Python so the query is backend-agnostic.
    rows = conn.execute(sa.text("SELECT accession_number FROM item")).fetchall()
    max_acc = 0
    for (acc,) in rows:
        try:
            max_acc = max(max_acc, int(acc))
        except (ValueError, TypeError):
            pass
    conn.execute(
        sa.text("INSERT INTO counters (key, value) VALUES ('catalog.accession', :v)"),
        {"v": max_acc},
    )


def downgrade() -> None:
    op.drop_table('counters')
