"""add_item_note

Revision ID: 443891bfaa50
Revises: 5b9e539aca65
Create Date: 2026-05-30 02:02:44.914558

Adds item_note table for per-copy notes and condition history entries.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '443891bfaa50'
down_revision: Union[str, Sequence[str], None] = '5b9e539aca65'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "item_note",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("is_system", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("actor_label", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"]),
    )
    op.create_index("ix_item_note_item_id", "item_note", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_item_note_item_id", table_name="item_note")
    op.drop_table("item_note")
