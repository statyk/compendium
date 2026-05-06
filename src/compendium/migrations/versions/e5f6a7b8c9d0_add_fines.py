"""add_fines

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-22 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'fine',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('patron_id', sa.Integer(), sa.ForeignKey('patron.id'), nullable=False, index=True),
        sa.Column('loan_id', sa.Integer(), sa.ForeignKey('loan.id'), nullable=True),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('item.id'), nullable=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('assessed_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('resolved_by_user_id', sa.Integer(), sa.ForeignKey('app_user.id'), nullable=True),
    )
    op.create_index('ix_fine_patron_status', 'fine', ['patron_id', 'status'])
    op.create_index('ix_fine_loan', 'fine', ['loan_id'])
    # Partial unique index: at most one outstanding overdue fine per loan.
    # Supported by both SQLite and Postgres.
    op.create_index(
        'ix_fine_overdue_uniq',
        'fine',
        ['loan_id'],
        unique=True,
        sqlite_where=sa.text("status = 'outstanding' AND kind = 'overdue'"),
        postgresql_where=sa.text("status = 'outstanding' AND kind = 'overdue'"),
    )

    with op.batch_alter_table('loan_policy', schema=None) as batch_op:
        batch_op.add_column(sa.Column('overdue_fine_per_day_cents', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('overdue_fine_cap_cents', sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                'grace_period_days',
                sa.Integer(),
                nullable=False,
                server_default='0',
            )
        )
        batch_op.add_column(sa.Column('lost_item_default_cents', sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column('lost_item_processing_fee_cents', sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('loan_policy', schema=None) as batch_op:
        batch_op.drop_column('lost_item_processing_fee_cents')
        batch_op.drop_column('lost_item_default_cents')
        batch_op.drop_column('grace_period_days')
        batch_op.drop_column('overdue_fine_cap_cents')
        batch_op.drop_column('overdue_fine_per_day_cents')

    op.drop_index('ix_fine_overdue_uniq', table_name='fine')
    op.drop_index('ix_fine_loan', table_name='fine')
    op.drop_index('ix_fine_patron_status', table_name='fine')
    op.drop_table('fine')
