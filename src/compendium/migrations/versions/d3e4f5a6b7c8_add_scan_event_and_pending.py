"""add_scan_event_and_pending

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-06-10 12:00:00.000000

Backs the phone-scanner UX revamp:
- scan_pairing.catalog_review: per-pairing "review first" flag.
- scan_event: append-only desk live-feed log (one row per non-ignored dispatch).
- scan_pending_item: catalog scans held for desk review. The metadata snapshot
  column is named ``meta_json`` (not ``metadata``) because ``metadata`` is a
  reserved attribute name on SQLAlchemy declarative models.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("scan_pairing") as batch_op:
        batch_op.add_column(
            sa.Column(
                "catalog_review", sa.Boolean(), nullable=False,
                server_default=sa.false(),
            )
        )

    op.create_table(
        "scan_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pairing_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("message", sa.String(255), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("patron_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(["pairing_id"], ["scan_pairing.id"]),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"]),
        sa.ForeignKeyConstraint(["patron_id"], ["patron.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("scan_event") as batch_op:
        batch_op.create_index("ix_scan_event_pairing_id", ["pairing_id"])

    op.create_table(
        "scan_pending_item",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pairing_id", sa.Integer(), nullable=False),
        sa.Column("isbn", sa.String(20), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("meta_json", sa.JSON(), nullable=False),
        sa.Column("cover_url", sa.String(1024), nullable=True),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("created_item_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["pairing_id"], ["scan_pairing.id"]),
        sa.ForeignKeyConstraint(["resolved_by"], ["app_user.id"]),
        sa.ForeignKeyConstraint(["created_item_id"], ["item.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("scan_pending_item") as batch_op:
        batch_op.create_index("ix_scan_pending_item_pairing_id", ["pairing_id"])


def downgrade() -> None:
    with op.batch_alter_table("scan_pending_item") as batch_op:
        batch_op.drop_index("ix_scan_pending_item_pairing_id")
    op.drop_table("scan_pending_item")
    with op.batch_alter_table("scan_event") as batch_op:
        batch_op.drop_index("ix_scan_event_pairing_id")
    op.drop_table("scan_event")
    with op.batch_alter_table("scan_pairing") as batch_op:
        batch_op.drop_column("catalog_review")
