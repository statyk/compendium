"""fine paid_cents

Revision ID: 062bf41491d3
Revises: 0c0bf7eed591
Create Date: 2026-07-13 05:03:14.568545

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '062bf41491d3'
down_revision: Union[str, Sequence[str], None] = '0c0bf7eed591'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "fine",
        sa.Column("paid_cents", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("fine", "paid_cents")
