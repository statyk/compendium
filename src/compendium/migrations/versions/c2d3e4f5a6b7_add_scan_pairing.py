"""add_scan_pairing

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-09 12:00:00.000000

Adds the scan_pairing table backing the remote phone-scanner feature: one row
per staff-owned paired phone session. Stores only the SHA-256 hex digest of the
current secret (token_hash), never the raw secret. Unique index on token_hash
for secret lookups; plain index on expires_at for the dep filter and prune job.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scan_pairing",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("allowed_modes", sa.JSON(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("borrower_patron_id", sa.Integer(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"]),
        sa.ForeignKeyConstraint(["borrower_patron_id"], ["patron.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("scan_pairing") as batch_op:
        batch_op.create_index("ix_scan_pairing_token_hash", ["token_hash"], unique=True)
        batch_op.create_index("ix_scan_pairing_expires_at", ["expires_at"])


def downgrade() -> None:
    with op.batch_alter_table("scan_pairing") as batch_op:
        batch_op.drop_index("ix_scan_pairing_expires_at")
        batch_op.drop_index("ix_scan_pairing_token_hash")
    op.drop_table("scan_pairing")
