"""--format coverage for the system group: audit, secrets, settings, metadata.

Pattern mirrors ``tests/integration/test_cli_format_admin.py``: patch
``session_scope`` in the command module under test to yield the shared
``session`` fixture, so the CLI runs against the same in-memory DB the rest of
the integration suite uses.

``audit.py``, ``secrets.py``, and ``settings.py`` import ``session_scope`` at
module scope, so patching ``compendium.cli.commands.<module>.session_scope``
works. ``metadata.py`` imports it *locally* inside each command function, so
its tests instead monkeypatch ``compendium.db.engine.get_engine`` (the same
pattern used by ``tests/integration/test_admin_settings.py``).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from compendium.cli.main import app
from compendium.domain.models import Base, MetadataCache


def _invoke(session, module: str, args: list[str]):
    @contextmanager
    def _scope():
        yield session

    runner = CliRunner()
    # NOTE: installed click (8.3.x) dropped CliRunner's mix_stderr kwarg —
    # stdout/stderr are separate streams by default now, which is exactly
    # what we want (result.stdout / result.stderr below).
    with patch(f"compendium.cli.commands.{module}.session_scope", _scope):
        return runner.invoke(app, args)


# ──────────────────────────────────────────────────────────────────────────
# settings
# ──────────────────────────────────────────────────────────────────────────


def test_settings_list_json(session, monkeypatch):
    # `_resolve_source_for_registry` opens its own Session against
    # `compendium.db.engine.get_engine()` (not the patched session_scope), so
    # it must be pointed at the same engine backing the shared `session`
    # fixture or it errors on a missing `site_setting` table.
    monkeypatch.setattr("compendium.db.engine.get_engine", lambda: session.get_bind())
    r = _invoke(session, "settings", ["settings", "list", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert {"key", "env_var", "scope", "source", "value"} <= set(data[0])
    row = next(row for row in data if row["key"] == "library_name")
    assert row["value"] == "Compendium"


def test_settings_list_json_masking_matches_table(session, monkeypatch):
    monkeypatch.setattr("compendium.db.engine.get_engine", lambda: session.get_bind())
    r_json = _invoke(
        session, "settings", ["settings", "list", "--all", "--format", "json"]
    )
    assert r_json.exit_code == 0, r_json.output
    data = json.loads(r_json.stdout)
    row = next(row for row in data if row["key"] == "jwt_secret_key")
    # Masked by default, same as the table path (--show-secrets not passed).
    assert row["value"] == "********"

    r_unmasked = _invoke(
        session,
        "settings",
        ["settings", "list", "--all", "--show-secrets", "--format", "json"],
    )
    data_unmasked = json.loads(r_unmasked.stdout)
    row_unmasked = next(
        row for row in data_unmasked if row["key"] == "jwt_secret_key"
    )
    assert row_unmasked["value"] != "********"


def test_settings_get_json(session):
    r = _invoke(
        session, "settings", ["settings", "get", "library_name", "--format", "json"]
    )
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data == {"key": "library_name", "value": "Compendium"}


def test_settings_get_table_raw(session):
    """Table output for `settings get` must stay the bare value, no labels."""
    r = _invoke(session, "settings", ["settings", "get", "library_name"])
    assert r.exit_code == 0, r.output
    assert r.stdout.strip() == "Compendium"


# ──────────────────────────────────────────────────────────────────────────
# audit
# ──────────────────────────────────────────────────────────────────────────


def test_audit_json_details_is_object(session):
    # `settings set` writes an audit entry with a dict `details` payload.
    r = _invoke(
        session,
        "settings",
        ["settings", "set", "library_name", "Riverdale Public"],
    )
    assert r.exit_code == 0, r.output

    r = _invoke(session, "audit", ["audit", "list", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data
    assert {
        "id",
        "occurred_at",
        "source",
        "actor",
        "entity_type",
        "entity_id",
        "action",
        "details",
    } <= set(data[0])
    assert isinstance(data[0]["details"], (dict, type(None)))


# ──────────────────────────────────────────────────────────────────────────
# secrets
# ──────────────────────────────────────────────────────────────────────────


def test_secrets_list_json(session):
    r = _invoke(session, "secrets", ["secrets", "list", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data
    assert {"key", "env_var", "source", "display_name"} <= set(data[0])


# ──────────────────────────────────────────────────────────────────────────
# metadata (cache stats / gb-quota) — local session_scope import, so patch
# the engine instead of the command module's session_scope attribute.
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def cache_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


def test_metadata_cache_stats_json(cache_engine, monkeypatch):
    monkeypatch.setattr("compendium.db.session.get_engine", lambda: cache_engine)
    r = CliRunner().invoke(app, ["metadata", "cache", "stats", "--format", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert set(data) == {
        "total",
        "positive",
        "negative",
        "expired_positive",
        "expired_negative",
        "oldest_fetched_at",
        "adapter_counts",
    }
    assert data["total"] == 0
    assert data["oldest_fetched_at"] is None
    assert data["adapter_counts"] == {}


def test_metadata_gb_quota_status_json_not_exhausted(cache_engine, monkeypatch):
    monkeypatch.setattr("compendium.db.session.get_engine", lambda: cache_engine)
    r = CliRunner().invoke(
        app, ["metadata", "gb-quota", "status", "--format", "json"]
    )
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data == {"exhausted": False, "hit_at": None, "auto_reset": False}


def test_metadata_gb_quota_status_json_exhausted(cache_engine, monkeypatch):
    monkeypatch.setattr("compendium.db.session.get_engine", lambda: cache_engine)
    factory = sessionmaker(bind=cache_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    s.add(
        MetadataCache(
            adapter="GoogleBooksAdapter",
            kind="_quota",
            lookup_value="exhausted",
            is_negative=True,
            payload=None,
            fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    s.commit()
    s.close()

    # `is_gb_quota_exhausted()`'s own threshold math is exercised elsewhere
    # (tests/unit/test_metadata_*); patch it directly so this test is only
    # about the `--format json` contract wiring, not quota-window math (it
    # has a pre-existing naive-vs-aware datetime comparison quirk when read
    # back through a fresh session that's orthogonal to this task).
    with patch(
        "compendium.services.metadata.is_gb_quota_exhausted", return_value=True
    ):
        r = CliRunner().invoke(
            app, ["metadata", "gb-quota", "status", "--format", "json"]
        )
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["exhausted"] is True
    assert data["hit_at"] is not None
    assert data["auto_reset"] is False
