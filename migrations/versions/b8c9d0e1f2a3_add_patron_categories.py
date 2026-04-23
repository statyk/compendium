"""add_patron_categories

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-04-23 14:00:00.000000

Adds patron categorization (adult/child/staff/teacher/...) plus card
expiry, and lets loan policies scope by patron category in addition to
media type. Existing patrons land with NULL category and NULL expiry
(no behavioral change). Existing policies stay (any patron) — i.e.
patron_category_id NULL.

Seeds four default categories: Adult (default), Child, Staff, Teacher.
The seed runs idempotently; if any category already exists, skips.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'patron_category',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(32), nullable=False, unique=True),
        sa.Column('display_name', sa.String(64), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    with op.batch_alter_table('patron') as batch:
        batch.add_column(sa.Column('category_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('expires_at', sa.Date(), nullable=True))
        batch.create_foreign_key(
            'fk_patron_category_id', 'patron_category', ['category_id'], ['id']
        )
        batch.create_index('ix_patron_category_id', ['category_id'])

    with op.batch_alter_table('loan_policy') as batch:
        batch.add_column(sa.Column('patron_category_id', sa.Integer(), nullable=True))
        batch.create_foreign_key(
            'fk_loan_policy_patron_category_id',
            'patron_category',
            ['patron_category_id'],
            ['id'],
        )
        batch.create_index(
            'ix_loan_policy_patron_category_id', ['patron_category_id']
        )

    # Seed defaults — idempotent: only insert codes not already present.
    bind = op.get_bind()
    pc = sa.table(
        'patron_category',
        sa.column('code', sa.String),
        sa.column('display_name', sa.String),
        sa.column('is_default', sa.Boolean),
    )
    existing = {
        row[0]
        for row in bind.execute(sa.text("SELECT code FROM patron_category")).fetchall()
    }
    rows = [
        {"code": "adult", "display_name": "Adult", "is_default": True},
        {"code": "child", "display_name": "Child", "is_default": False},
        {"code": "staff", "display_name": "Staff", "is_default": False},
        {"code": "teacher", "display_name": "Teacher", "is_default": False},
    ]
    rows = [r for r in rows if r["code"] not in existing]
    if rows:
        op.bulk_insert(pc, rows)


def downgrade() -> None:
    with op.batch_alter_table('loan_policy') as batch:
        batch.drop_index('ix_loan_policy_patron_category_id')
        batch.drop_constraint('fk_loan_policy_patron_category_id', type_='foreignkey')
        batch.drop_column('patron_category_id')

    with op.batch_alter_table('patron') as batch:
        batch.drop_index('ix_patron_category_id')
        batch.drop_constraint('fk_patron_category_id', type_='foreignkey')
        batch.drop_column('expires_at')
        batch.drop_column('category_id')

    op.drop_table('patron_category')
