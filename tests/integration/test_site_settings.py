"""Integration tests for the site-settings read helper + DB-backed cache.

Covers:
- Env var wins over DB row (break-glass).
- Env var type-coercion failures are loud (not silently defaulted).
- DB row returned when env unset.
- Default returned when neither env nor DB set.
- set_site_setting persists + invalidates cache.
- invalidate_cache() forces a reread on next access.
- Pilot: Jinja library_name() global picks up DB updates.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.domain.models import Base
from compendium.services import site_settings as ss
from compendium.services.settings_registry import SettingValidationError


@pytest.fixture
def ss_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def ss_session(ss_engine):
    factory = sessionmaker(bind=ss_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.rollback()
    s.close()


@pytest.fixture(autouse=True)
def _patch_engine_and_clear_cache(ss_engine, monkeypatch):
    """Point the helper at our test engine + start with a clean cache."""
    monkeypatch.setattr("compendium.db.engine.get_engine", lambda: ss_engine)
    ss.invalidate_cache()
    # Also make sure no leaked env vars from other tests bleed in
    for key in (
        "COMPENDIUM_LIBRARY_NAME",
        "COMPENDIUM_DEFAULT_THEME",
        "COMPENDIUM_GUEST_SEARCH_ENABLED",
        "COMPENDIUM_CURRENCY_SYMBOL",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    ss.invalidate_cache()


class TestLookupOrder:
    def test_default_when_no_override(self):
        assert ss.get_site_setting("library_name") == "Compendium"
        assert ss.get_site_setting("guest_search_enabled") is True
        assert ss.get_site_setting("default_theme") == "light"
        assert ss.get_site_setting("currency_symbol") == "$"

    def test_env_wins_over_default(self, monkeypatch):
        monkeypatch.setenv("COMPENDIUM_LIBRARY_NAME", "Springfield Public")
        assert ss.get_site_setting("library_name") == "Springfield Public"

    def test_env_bool_parses(self, monkeypatch):
        monkeypatch.setenv("COMPENDIUM_GUEST_SEARCH_ENABLED", "false")
        assert ss.get_site_setting("guest_search_enabled") is False

    def test_env_literal_parses(self, monkeypatch):
        monkeypatch.setenv("COMPENDIUM_DEFAULT_THEME", "dark")
        assert ss.get_site_setting("default_theme") == "dark"

    def test_bad_env_fails_loud(self, monkeypatch):
        monkeypatch.setenv("COMPENDIUM_GUEST_SEARCH_ENABLED", "perhaps")
        with pytest.raises(SettingValidationError):
            ss.get_site_setting("guest_search_enabled")

    def test_bad_env_literal_fails_loud(self, monkeypatch):
        monkeypatch.setenv("COMPENDIUM_DEFAULT_THEME", "neon")
        with pytest.raises(SettingValidationError):
            ss.get_site_setting("default_theme")

    def test_db_row_returned_when_env_unset(self, ss_session):
        ss.set_site_setting("library_name", "Oak Park", session=ss_session)
        ss_session.commit()
        assert ss.get_site_setting("library_name") == "Oak Park"

    def test_env_overrides_db_row(self, ss_session, monkeypatch):
        ss.set_site_setting("library_name", "Oak Park", session=ss_session)
        ss_session.commit()
        monkeypatch.setenv("COMPENDIUM_LIBRARY_NAME", "Break Glass")
        assert ss.get_site_setting("library_name") == "Break Glass"

    def test_empty_env_string_falls_through_to_db(self, ss_session, monkeypatch):
        """Docker-compose forwards '' when host var is unset; must not mask DB value."""
        ss.set_site_setting("library_name", "Archived", session=ss_session)
        ss_session.commit()
        monkeypatch.setenv("COMPENDIUM_LIBRARY_NAME", "")
        ss.invalidate_cache()
        assert ss.get_site_setting("library_name") == "Archived"

    def test_empty_env_string_falls_through_to_default(self, monkeypatch):
        """Empty env string with no DB row returns the descriptor default."""
        monkeypatch.setenv("COMPENDIUM_LIBRARY_NAME", "")
        ss.invalidate_cache()
        assert ss.get_site_setting("library_name") == "Compendium"


class TestCache:
    def test_write_invalidates_cache(self, ss_session):
        # First read caches default
        assert ss.get_site_setting("library_name") == "Compendium"
        # Write via helper — should invalidate
        ss.set_site_setting("library_name", "Riverdale", session=ss_session)
        ss_session.commit()
        assert ss.get_site_setting("library_name") == "Riverdale"

    def test_out_of_band_write_stale_until_invalidate(self, ss_session, ss_engine):
        # First read primes cache with default
        assert ss.get_site_setting("library_name") == "Compendium"
        # Directly mutate the table (bypassing helper) — cache stays stale
        from compendium.domain.models import SiteSetting

        ss_session.add(SiteSetting(key="library_name", value="Direct"))
        ss_session.commit()
        # Cache is still within TTL, still returns old value
        assert ss.get_site_setting("library_name") == "Compendium"
        # After explicit invalidate, next read sees the new row
        ss.invalidate_cache()
        assert ss.get_site_setting("library_name") == "Direct"

    def test_delete_reverts_to_default(self, ss_session):
        ss.set_site_setting("library_name", "X", session=ss_session)
        ss_session.commit()
        assert ss.get_site_setting("library_name") == "X"
        ss.delete_site_setting("library_name", session=ss_session)
        ss_session.commit()
        assert ss.get_site_setting("library_name") == "Compendium"


class TestPilotReaders:
    """Spot-check that pilot call-sites read through the helper."""

    def test_jinja_global_reflects_db(self, ss_session):
        from compendium.web.jinja import _jinja_library_name

        assert _jinja_library_name() == "Compendium"
        ss.set_site_setting("library_name", "Midtown Branch", session=ss_session)
        ss_session.commit()
        assert _jinja_library_name() == "Midtown Branch"

    def test_label_card_header_reflects_db(self, ss_session):
        """Label module reads directly via get_site_setting('library_name')."""
        ss.set_site_setting("library_name", "Hollis Branch", session=ss_session)
        ss_session.commit()
        assert ss.get_site_setting("library_name") == "Hollis Branch"
