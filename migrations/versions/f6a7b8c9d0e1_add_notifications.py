"""add_notifications

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-04-22 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'notification',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('recipient_patron_id', sa.Integer(), sa.ForeignKey('patron.id'), nullable=True),
        sa.Column('recipient_email', sa.String(256), nullable=True),
        sa.Column('template_key', sa.String(32), nullable=False),
        sa.Column('context', sa.JSON(), nullable=False),
        sa.Column('subject', sa.Text(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('loan_id', sa.Integer(), sa.ForeignKey('loan.id'), nullable=True),
        sa.Column('hold_id', sa.Integer(), sa.ForeignKey('hold.id'), nullable=True),
        sa.Column('discriminator', sa.Integer(), nullable=False, server_default='0'),
        sa.Column(
            'scheduled_for',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index('ix_notification_status', 'notification', ['status'])
    op.create_index(
        'ix_notification_scheduled',
        'notification',
        ['scheduled_for'],
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        'ix_notification_loan_dedup',
        'notification',
        ['loan_id', 'template_key', 'discriminator'],
        unique=True,
        sqlite_where=sa.text("loan_id IS NOT NULL AND status != 'cancelled'"),
        postgresql_where=sa.text("loan_id IS NOT NULL AND status != 'cancelled'"),
    )
    op.create_index(
        'ix_notification_hold_dedup',
        'notification',
        ['hold_id', 'template_key', 'discriminator'],
        unique=True,
        sqlite_where=sa.text("hold_id IS NOT NULL AND status != 'cancelled'"),
        postgresql_where=sa.text("hold_id IS NOT NULL AND status != 'cancelled'"),
    )

    with op.batch_alter_table('patron', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'receive_notifications',
                sa.Boolean(),
                nullable=False,
                server_default='1',
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('patron', schema=None) as batch_op:
        batch_op.drop_column('receive_notifications')

    op.drop_index('ix_notification_hold_dedup', table_name='notification')
    op.drop_index('ix_notification_loan_dedup', table_name='notification')
    op.drop_index('ix_notification_scheduled', table_name='notification')
    op.drop_index('ix_notification_status', table_name='notification')
    op.drop_table('notification')
