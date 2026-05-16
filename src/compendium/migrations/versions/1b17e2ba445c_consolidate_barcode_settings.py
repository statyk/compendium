"""consolidate_barcode_settings

Revision ID: 1b17e2ba445c
Revises: a1b2c3d4e5f6
Create Date: 2026-05-15 20:45:53.185051

Consolidates the legacy ``barcode_length`` and ``barcode_location_enabled``
site_setting rows into a single ``barcode_format`` value ("10-digit" or
"14-digit").

If neither old row exists (fresh install), no row is written — the registry
default of "10-digit" takes effect automatically.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '1b17e2ba445c'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge barcode_length + barcode_location_enabled → barcode_format."""
    conn = op.get_bind()

    row = conn.execute(
        sa.text(
            "SELECT value FROM site_setting WHERE key = 'barcode_location_enabled'"
        )
    ).fetchone()

    if row is not None:
        barcode_format = (
            "14-digit"
            if row[0].lower() in ("true", "1", "yes")
            else "10-digit"
        )
        # Delete any pre-existing barcode_format row before inserting so this
        # works on both SQLite (INSERT OR REPLACE) and Postgres.
        conn.execute(
            sa.text("DELETE FROM site_setting WHERE key = 'barcode_format'")
        )
        conn.execute(
            sa.text(
                "INSERT INTO site_setting (key, value) VALUES ('barcode_format', :v)"
            ),
            {"v": barcode_format},
        )

    conn.execute(
        sa.text(
            "DELETE FROM site_setting"
            " WHERE key IN ('barcode_length', 'barcode_location_enabled')"
        )
    )


def downgrade() -> None:
    """Split barcode_format back into barcode_length + barcode_location_enabled."""
    conn = op.get_bind()

    row = conn.execute(
        sa.text("SELECT value FROM site_setting WHERE key = 'barcode_format'")
    ).fetchone()

    if row is not None:
        is_14 = row[0] == "14-digit"
        location_enabled = "true" if is_14 else "false"
        barcode_length = "14" if is_14 else "10"

        conn.execute(
            sa.text("DELETE FROM site_setting WHERE key = 'barcode_format'")
        )
        conn.execute(
            sa.text(
                "INSERT INTO site_setting (key, value)"
                " VALUES ('barcode_location_enabled', :loc), ('barcode_length', :bl)"
            ),
            {"loc": location_enabled, "bl": barcode_length},
        )
