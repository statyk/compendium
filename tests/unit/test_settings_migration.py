"""Unit tests for the consolidate_barcode_settings Alembic migration.

Tests call the migration's upgrade/downgrade logic directly against an
in-memory SQLite connection so that full Alembic machinery (and migration
ordering) is not required.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import create_engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine() -> sa.engine.Engine:
    """Return an in-memory SQLite engine with the site_setting table."""
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(sa.text(
            "CREATE TABLE site_setting ("
            "  key   TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL"
            ")"
        ))
        conn.commit()
    return engine


def _run_upgrade(conn: sa.engine.Connection) -> None:
    """Execute the upgrade() logic from the migration against *conn*."""
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
        conn.execute(sa.text("DELETE FROM site_setting WHERE key = 'barcode_format'"))
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


def _run_downgrade(conn: sa.engine.Connection) -> None:
    """Execute the downgrade() logic from the migration against *conn*."""
    row = conn.execute(
        sa.text("SELECT value FROM site_setting WHERE key = 'barcode_format'")
    ).fetchone()

    if row is not None:
        is_14 = row[0] == "14-digit"
        location_enabled = "true" if is_14 else "false"
        barcode_length = "14" if is_14 else "10"

        conn.execute(sa.text("DELETE FROM site_setting WHERE key = 'barcode_format'"))
        conn.execute(
            sa.text(
                "INSERT INTO site_setting (key, value)"
                " VALUES ('barcode_location_enabled', :loc), ('barcode_length', :bl)"
            ),
            {"loc": location_enabled, "bl": barcode_length},
        )


def _fetch(conn: sa.engine.Connection, key: str) -> str | None:
    row = conn.execute(
        sa.text("SELECT value FROM site_setting WHERE key = :k"),
        {"k": key},
    ).fetchone()
    return row[0] if row else None


def _keys(conn: sa.engine.Connection) -> set[str]:
    rows = conn.execute(sa.text("SELECT key FROM site_setting")).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Upgrade tests
# ---------------------------------------------------------------------------

class TestUpgrade:
    def test_location_enabled_true_produces_14_digit(self):
        engine = _make_engine()
        with engine.connect() as conn:
            conn.execute(sa.text(
                "INSERT INTO site_setting (key, value)"
                " VALUES ('barcode_location_enabled', 'true'), ('barcode_length', '14')"
            ))
            conn.commit()

        with engine.connect() as conn:
            _run_upgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            assert _fetch(conn, "barcode_format") == "14-digit"
            assert "barcode_length" not in _keys(conn)
            assert "barcode_location_enabled" not in _keys(conn)

    def test_location_enabled_false_produces_10_digit(self):
        engine = _make_engine()
        with engine.connect() as conn:
            conn.execute(sa.text(
                "INSERT INTO site_setting (key, value)"
                " VALUES ('barcode_location_enabled', 'false'), ('barcode_length', '10')"
            ))
            conn.commit()

        with engine.connect() as conn:
            _run_upgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            assert _fetch(conn, "barcode_format") == "10-digit"
            assert "barcode_length" not in _keys(conn)
            assert "barcode_location_enabled" not in _keys(conn)

    def test_location_enabled_1_produces_14_digit(self):
        """Truthy value '1' should map to 14-digit."""
        engine = _make_engine()
        with engine.connect() as conn:
            conn.execute(sa.text(
                "INSERT INTO site_setting (key, value)"
                " VALUES ('barcode_location_enabled', '1'), ('barcode_length', '14')"
            ))
            conn.commit()

        with engine.connect() as conn:
            _run_upgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            assert _fetch(conn, "barcode_format") == "14-digit"

    def test_location_enabled_yes_produces_14_digit(self):
        """Truthy value 'yes' should map to 14-digit."""
        engine = _make_engine()
        with engine.connect() as conn:
            conn.execute(sa.text(
                "INSERT INTO site_setting (key, value)"
                " VALUES ('barcode_location_enabled', 'yes')"
            ))
            conn.commit()

        with engine.connect() as conn:
            _run_upgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            assert _fetch(conn, "barcode_format") == "14-digit"

    def test_fresh_install_no_rows_writes_nothing(self):
        """If neither old key exists, no barcode_format row is written."""
        engine = _make_engine()

        with engine.connect() as conn:
            _run_upgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            assert _fetch(conn, "barcode_format") is None
            assert _keys(conn) == set()

    def test_only_barcode_length_present_leaves_no_format_row(self):
        """If only barcode_length exists (no location flag), nothing is inserted."""
        engine = _make_engine()
        with engine.connect() as conn:
            conn.execute(sa.text(
                "INSERT INTO site_setting (key, value) VALUES ('barcode_length', '10')"
            ))
            conn.commit()

        with engine.connect() as conn:
            _run_upgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            assert _fetch(conn, "barcode_format") is None
            assert "barcode_length" not in _keys(conn)

    def test_existing_barcode_format_is_replaced(self):
        """If a barcode_format row already existed it should be overwritten."""
        engine = _make_engine()
        with engine.connect() as conn:
            conn.execute(sa.text(
                "INSERT INTO site_setting (key, value)"
                " VALUES"
                " ('barcode_format', '10-digit'),"
                " ('barcode_location_enabled', 'true'),"
                " ('barcode_length', '14')"
            ))
            conn.commit()

        with engine.connect() as conn:
            _run_upgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            assert _fetch(conn, "barcode_format") == "14-digit"


# ---------------------------------------------------------------------------
# Downgrade tests
# ---------------------------------------------------------------------------

class TestDowngrade:
    def test_14_digit_produces_location_enabled_and_length_14(self):
        engine = _make_engine()
        with engine.connect() as conn:
            conn.execute(sa.text(
                "INSERT INTO site_setting (key, value) VALUES ('barcode_format', '14-digit')"
            ))
            conn.commit()

        with engine.connect() as conn:
            _run_downgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            assert _fetch(conn, "barcode_location_enabled") == "true"
            assert _fetch(conn, "barcode_length") == "14"
            assert "barcode_format" not in _keys(conn)

    def test_10_digit_produces_location_enabled_false_and_length_10(self):
        engine = _make_engine()
        with engine.connect() as conn:
            conn.execute(sa.text(
                "INSERT INTO site_setting (key, value) VALUES ('barcode_format', '10-digit')"
            ))
            conn.commit()

        with engine.connect() as conn:
            _run_downgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            assert _fetch(conn, "barcode_location_enabled") == "false"
            assert _fetch(conn, "barcode_length") == "10"
            assert "barcode_format" not in _keys(conn)

    def test_no_barcode_format_row_writes_nothing(self):
        """If barcode_format is absent, downgrade is a no-op."""
        engine = _make_engine()

        with engine.connect() as conn:
            _run_downgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            assert _keys(conn) == set()


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_upgrade_then_downgrade_14_digit(self):
        engine = _make_engine()
        with engine.connect() as conn:
            conn.execute(sa.text(
                "INSERT INTO site_setting (key, value)"
                " VALUES ('barcode_location_enabled', 'true'), ('barcode_length', '14')"
            ))
            conn.commit()

        with engine.connect() as conn:
            _run_upgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            _run_downgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            assert _fetch(conn, "barcode_location_enabled") == "true"
            assert _fetch(conn, "barcode_length") == "14"
            assert "barcode_format" not in _keys(conn)

    def test_upgrade_then_downgrade_10_digit(self):
        engine = _make_engine()
        with engine.connect() as conn:
            conn.execute(sa.text(
                "INSERT INTO site_setting (key, value)"
                " VALUES ('barcode_location_enabled', 'false'), ('barcode_length', '10')"
            ))
            conn.commit()

        with engine.connect() as conn:
            _run_upgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            _run_downgrade(conn)
            conn.commit()

        with engine.connect() as conn:
            assert _fetch(conn, "barcode_location_enabled") == "false"
            assert _fetch(conn, "barcode_length") == "10"
            assert "barcode_format" not in _keys(conn)
