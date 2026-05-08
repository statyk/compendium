"""Integration tests for slice C — settings CLI, API, and web UI.

The five web pages render as expected for librarian / system-tier users,
form submission updates the DB, env-overridden rows are flagged, the API
respects per-key scope, and the CLI prints + writes correctly.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from compendium.api.app import create_app
from compendium.cli.main import app as cli_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services import site_settings as ss
from compendium.services.auth import AuthService, hash_password
from compendium.services.site_settings import (
    delete_site_setting,
    get_site_setting,
    set_site_setting,
)
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _sign, generate_token

_SETTINGS = Settings(database_url="sqlite:///:memory:")


@pytest.fixture
def s_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def s_session(s_engine):
    factory = sessionmaker(bind=s_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture(autouse=True)
def _settings_isolation(s_engine, monkeypatch):
    """Point site_settings helper at our test engine + clean cache + clean env."""
    monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
    ss.invalidate_cache()
    for var in (
        "COMPENDIUM_LIBRARY_NAME",
        "COMPENDIUM_DEFAULT_THEME",
        "COMPENDIUM_GUEST_SEARCH_ENABLED",
        "COMPENDIUM_CURRENCY_SYMBOL",
        "COMPENDIUM_FINE_BLOCK_THRESHOLD_CENTS",
        "COMPENDIUM_FINE_BLOCK_HOLDS",
        "COMPENDIUM_KIOSK_IDLE_TIMEOUT_SECONDS",
        "COMPENDIUM_HOLD_EXPIRY_DAYS",
        "COMPENDIUM_HOLD_PICKUP_DAYS",
        "COMPENDIUM_OVERDUE_TIERS",
        "COMPENDIUM_DUE_SOON_DAYS_BEFORE",
        "COMPENDIUM_SMTP_HOST",
        "COMPENDIUM_SMTP_PORT",
        "COMPENDIUM_AUDIT_RETENTION_DAYS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    ss.invalidate_cache()


def _make_user(s, *, role_name: str, username: str) -> tuple[AppUser, str]:
    role = SqlRoleRepository(s).get_by_name(role_name)
    u = AppUser(
        username=username,
        password_hash=hash_password("pw"),
        role_id=role.id,
    )
    SqlUserRepository(s).add(u)
    s.commit()
    u.role = role
    token = AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(u)
    return u, token


def _make_csrf_pair() -> tuple[str, str]:
    """Returns (raw_token, signed_cookie_value) for test requests."""
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _SETTINGS.jwt_secret_key)}"
    return raw, signed


@pytest.fixture
def client(s_engine, s_session):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=s_engine, autoflush=False, expire_on_commit=False)
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_session] = _override
    with patch("compendium.db.engine.get_settings", return_value=_SETTINGS):
        with TestClient(app, follow_redirects=False) as c:
            yield c


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────


class TestSettingsCli:
    def test_list_includes_known_keys(self, s_engine, monkeypatch):
        monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
        r = CliRunner().invoke(cli_app, ["settings", "list"])
        assert r.exit_code == 0, r.output
        assert "library_name" in r.output
        assert "smtp_host" in r.output

    def test_list_filters_by_scope(self, s_engine, monkeypatch):
        monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
        r = CliRunner().invoke(cli_app, ["settings", "list", "--scope", "system"])
        assert r.exit_code == 0
        assert "smtp_host" in r.output
        # Librarian-tier key should be filtered out
        assert "library_name" not in r.output

    def test_get_unknown_key_errors(self, s_engine, monkeypatch):
        monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
        r = CliRunner().invoke(cli_app, ["settings", "get", "nonexistent"])
        assert r.exit_code == 1

    def test_set_then_get_roundtrip(self, s_engine, monkeypatch, s_session):
        monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
        # Patch session_scope to use our test session
        from contextlib import contextmanager

        @contextmanager
        def fake_scope():
            yield s_session
            s_session.commit()

        monkeypatch.setattr(
            "compendium.cli.commands.settings.session_scope", fake_scope
        )
        r = CliRunner().invoke(
            cli_app, ["settings", "set", "library_name", "Riverdale Public"]
        )
        assert r.exit_code == 0, r.output
        ss.invalidate_cache()
        r = CliRunner().invoke(cli_app, ["settings", "get", "library_name"])
        assert "Riverdale Public" in r.output

    def test_list_default_excludes_env_only(self, s_engine, monkeypatch):
        monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
        r = CliRunner().invoke(cli_app, ["settings", "list"])
        assert r.exit_code == 0, r.output
        # Registry items present
        assert "library_name" in r.output
        assert "smtp_host" in r.output
        # Env-only items absent without --all
        assert "jwt_secret_key" not in r.output
        assert "database_url" not in r.output

    def test_list_all_includes_env_only(self, s_engine, monkeypatch):
        monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
        r = CliRunner().invoke(cli_app, ["settings", "list", "--all"])
        assert r.exit_code == 0, r.output
        # Both kinds are listed
        assert "library_name" in r.output  # registry
        assert "jwt_secret_key" in r.output  # env-only
        assert "database_url" in r.output  # env-only
        assert "default_loan_period_days" in r.output  # env-only (not yet migrated)
        # New ENV VAR column rendered
        assert "COMPENDIUM_LIBRARY_NAME" in r.output
        assert "COMPENDIUM_JWT_SECRET_KEY" in r.output
        # New env-only scope label
        assert "env-only" in r.output

    def test_list_all_masks_secrets_by_default(self, s_engine, monkeypatch):
        monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
        r = CliRunner().invoke(cli_app, ["settings", "list", "--all"])
        assert r.exit_code == 0, r.output
        # Sensitive defaults are masked
        for line in r.output.splitlines():
            if line.startswith("jwt_secret_key"):
                assert "********" in line, line
                assert "insecure-default" not in line, line
            if line.startswith("database_url"):
                assert "********" in line, line
                assert "sqlite" not in line, line

    def test_list_all_show_secrets_unmasks(self, s_engine, monkeypatch):
        monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
        r = CliRunner().invoke(
            cli_app, ["settings", "list", "--all", "--show-secrets"]
        )
        assert r.exit_code == 0, r.output
        assert "********" not in r.output
        assert "insecure-default-change-in-production" in r.output

    def test_list_scope_env_only(self, s_engine, monkeypatch):
        monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
        r = CliRunner().invoke(
            cli_app, ["settings", "list", "--scope", "env-only"]
        )
        assert r.exit_code == 0, r.output
        assert "jwt_secret_key" in r.output
        assert "database_url" in r.output
        # Registry items filtered out
        assert "library_name" not in r.output
        assert "smtp_host" not in r.output

    def test_list_source_db_after_set(self, s_engine, monkeypatch, s_session):
        """A registry key with a site_setting row should report source=db."""
        monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
        from contextlib import contextmanager

        @contextmanager
        def fake_scope():
            yield s_session
            s_session.commit()

        monkeypatch.setattr(
            "compendium.cli.commands.settings.session_scope", fake_scope
        )
        # Write a row, then list should show source=db for that key.
        CliRunner().invoke(
            cli_app, ["settings", "set", "library_name", "Riverdale Public"]
        )
        ss.invalidate_cache()
        r = CliRunner().invoke(cli_app, ["settings", "list"])
        assert r.exit_code == 0, r.output
        for line in r.output.splitlines():
            if line.startswith("library_name"):
                assert " db " in f" {line} ", line
                break
        else:
            pytest.fail("library_name not in list output")


# ──────────────────────────────────────────────────────────────────────────
# API
# ──────────────────────────────────────────────────────────────────────────


class TestSettingsApi:
    def test_list_requires_auth(self, client):
        r = client.get("/settings/")
        assert r.status_code in (401, 403)

    def test_librarian_sees_librarian_keys_only(self, client, s_session):
        # Use the slimmed Librarian preset (no system.manage)
        _, tok = _make_user(s_session, role_name="Librarian", username="lib_settings")
        r = client.get("/settings/", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        keys = {row["key"] for row in r.json()}
        assert "library_name" in keys
        assert "smtp_host" not in keys

    def test_admin_sees_all(self, client, s_session):
        _, tok = _make_user(
            s_session, role_name="Administrator", username="admin_settings"
        )
        r = client.get("/settings/", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        keys = {row["key"] for row in r.json()}
        assert "library_name" in keys
        assert "smtp_host" in keys

    def test_get_system_key_denied_to_librarian(self, client, s_session):
        _, tok = _make_user(s_session, role_name="Librarian", username="lib_g")
        r = client.get(
            "/settings/smtp_host", headers={"Authorization": f"Bearer {tok}"}
        )
        assert r.status_code == 403

    def test_patch_writes_and_returns_value(self, client, s_session):
        _, tok = _make_user(
            s_session, role_name="Administrator", username="admin_patch"
        )
        r = client.patch(
            "/settings/library_name",
            json={"value": "Hollis"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["value"] == "Hollis"
        # Reading again confirms persistence
        ss.invalidate_cache()
        assert get_site_setting("library_name") == "Hollis"

    def test_patch_invalid_value_422(self, client, s_session):
        _, tok = _make_user(
            s_session, role_name="Administrator", username="admin_invalid"
        )
        r = client.patch(
            "/settings/smtp_port",
            json={"value": 999999},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 422

    def test_patch_system_key_denied_to_librarian(self, client, s_session):
        _, tok = _make_user(s_session, role_name="Librarian", username="lib_p")
        r = client.patch(
            "/settings/smtp_host",
            json={"value": "smtp.example.com"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 403

    def test_delete_resets_to_default(self, client, s_session):
        _, tok = _make_user(
            s_session, role_name="Administrator", username="admin_reset"
        )
        # First write a value
        client.patch(
            "/settings/library_name",
            json={"value": "X"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        # Then reset
        r = client.delete(
            "/settings/library_name", headers={"Authorization": f"Bearer {tok}"}
        )
        assert r.status_code == 200
        assert r.json()["value"] == "Compendium"


# ──────────────────────────────────────────────────────────────────────────
# Web UI
# ──────────────────────────────────────────────────────────────────────────


def _login_cookies(client: TestClient, username: str) -> dict:
    raw, signed = _make_csrf_pair()
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": "pw", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303, resp.text
    return dict(resp.cookies)


class TestSettingsWeb:
    def test_general_page_renders_for_librarian(self, client, s_session):
        _make_user(s_session, role_name="Librarian", username="lib_web_g")
        cookies = _login_cookies(client, "lib_web_g")
        r = client.get("/ui/admin/settings/general", cookies=cookies)
        assert r.status_code == 200
        assert b"library_name" in r.content
        assert b"default_theme" in r.content

    def test_circulation_page_renders(self, client, s_session):
        _make_user(s_session, role_name="Librarian", username="lib_web_c")
        cookies = _login_cookies(client, "lib_web_c")
        r = client.get("/ui/admin/settings/circulation", cookies=cookies)
        assert r.status_code == 200
        assert b"currency_symbol" in r.content
        assert b"overdue_tiers" in r.content

    def test_kiosk_page_renders(self, client, s_session):
        _make_user(s_session, role_name="Librarian", username="lib_web_k")
        cookies = _login_cookies(client, "lib_web_k")
        r = client.get("/ui/admin/settings/kiosk", cookies=cookies)
        assert r.status_code == 200
        assert b"kiosk_idle_timeout_seconds" in r.content

    def test_identifiers_page_includes_barcode_symbology(self, client, s_session):
        """The Identifiers & Barcodes settings page must surface the new
        barcode_symbology setting registered in the same slice — registering
        it isn't enough; it has to appear in _PAGES["identifiers"]["keys"].
        """
        _make_user(s_session, role_name="Librarian", username="lib_web_i")
        cookies = _login_cookies(client, "lib_web_i")
        r = client.get("/ui/admin/settings/identifiers", cookies=cookies)
        assert r.status_code == 200
        assert b'name="barcode_symbology"' in r.content
        # All three Literal options should be present as <option> values.
        assert b'value="codabar"' in r.content
        assert b'value="code39"' in r.content
        assert b'value="code128"' in r.content

    def test_smtp_page_denied_to_librarian(self, client, s_session):
        _make_user(s_session, role_name="Librarian", username="lib_smtp_denied")
        cookies = _login_cookies(client, "lib_smtp_denied")
        r = client.get("/ui/admin/system/smtp", cookies=cookies)
        assert r.status_code in (302, 303, 403)

    def test_smtp_page_renders_for_admin(self, client, s_session):
        _make_user(s_session, role_name="Administrator", username="admin_smtp")
        cookies = _login_cookies(client, "admin_smtp")
        r = client.get("/ui/admin/system/smtp", cookies=cookies)
        assert r.status_code == 200
        assert b"smtp_host" in r.content
        # SMTP password row should not exist as an input
        assert b'name="smtp_password"' not in r.content

    def test_retention_page_renders_for_admin(self, client, s_session):
        _make_user(s_session, role_name="Administrator", username="admin_ret")
        cookies = _login_cookies(client, "admin_ret")
        r = client.get("/ui/admin/system/retention", cookies=cookies)
        assert r.status_code == 200
        assert b"audit_retention_days" in r.content

    def test_post_writes_setting(self, client, s_session):
        _make_user(s_session, role_name="Librarian", username="lib_post")
        cookies = _login_cookies(client, "lib_post")
        raw, signed = _make_csrf_pair()
        r = client.post(
            "/ui/admin/settings/general",
            data={
                "csrf_token": raw,
                "library_name": "Mid-Town",
                "default_theme": "dark",
                "guest_search_enabled": "true",
            },
            cookies={**cookies, CSRF_COOKIE: signed},
        )
        assert r.status_code == 303
        ss.invalidate_cache()
        assert get_site_setting("library_name") == "Mid-Town"
        assert get_site_setting("default_theme") == "dark"
        assert get_site_setting("guest_search_enabled") is True

    def test_post_unchecked_bool_persists_false(self, client, s_session):
        _make_user(s_session, role_name="Librarian", username="lib_bool")
        cookies = _login_cookies(client, "lib_bool")
        raw, signed = _make_csrf_pair()
        # Submit without the checkbox — should mean False for a checkbox
        r = client.post(
            "/ui/admin/settings/general",
            data={
                "csrf_token": raw,
                "library_name": "X",
                "default_theme": "light",
                # guest_search_enabled NOT submitted
            },
            cookies={**cookies, CSRF_COOKIE: signed},
        )
        assert r.status_code == 303
        ss.invalidate_cache()
        assert get_site_setting("guest_search_enabled") is False

    def test_reset_button_clears_override(self, client, s_session):
        _make_user(s_session, role_name="Librarian", username="lib_reset")
        cookies = _login_cookies(client, "lib_reset")
        # First write a value
        set_site_setting("library_name", "ToReset", session=s_session)
        s_session.commit()
        ss.invalidate_cache()
        assert get_site_setting("library_name") == "ToReset"
        # Now POST with reset
        raw, signed = _make_csrf_pair()
        r = client.post(
            "/ui/admin/settings/general",
            data={
                "csrf_token": raw,
                "reset": "library_name",
                "default_theme": "light",
            },
            cookies={**cookies, CSRF_COOKIE: signed},
        )
        assert r.status_code == 303
        ss.invalidate_cache()
        assert get_site_setting("library_name") == "Compendium"

    def test_env_override_indicator_shown(self, client, s_session, monkeypatch):
        monkeypatch.setenv("COMPENDIUM_LIBRARY_NAME", "ENV-Forced")
        ss.invalidate_cache()
        _make_user(s_session, role_name="Librarian", username="lib_env")
        cookies = _login_cookies(client, "lib_env")
        r = client.get("/ui/admin/settings/general", cookies=cookies)
        assert r.status_code == 200
        assert b"set by environment variable" in r.content
        assert b"COMPENDIUM_LIBRARY_NAME" in r.content
        assert b"Contact your system administrator" in r.content


# ──────────────────────────────────────────────────────────────────────────
# Audit emission
# ──────────────────────────────────────────────────────────────────────────


class TestSettingsAudit:
    def test_set_emits_audit(self, s_session):
        from compendium.repositories.sql.audit_log_repository import (
            SqlAuditLogRepository,
        )
        from compendium.services.audit import (
            AuditAction,
            AuditEntityType,
            AuditService,
        )

        audit = AuditService(SqlAuditLogRepository(s_session))
        set_site_setting(
            "library_name",
            "Audited",
            session=s_session,
            audit_svc=audit,
            actor_label="test",
            source="test",
        )
        s_session.commit()
        entries = audit.list(entity_type=AuditEntityType.SITE_SETTING)
        assert any(
            e.action == AuditAction.SETTING_UPDATE
            and e.details.get("key") == "library_name"
            and e.details.get("after")
            for e in entries
        )

    def test_reset_emits_audit(self, s_session):
        from compendium.repositories.sql.audit_log_repository import (
            SqlAuditLogRepository,
        )
        from compendium.services.audit import (
            AuditAction,
            AuditEntityType,
            AuditService,
        )

        set_site_setting("library_name", "X", session=s_session)
        s_session.commit()
        audit = AuditService(SqlAuditLogRepository(s_session))
        delete_site_setting(
            "library_name",
            session=s_session,
            audit_svc=audit,
            actor_label="test",
            source="test",
        )
        s_session.commit()
        entries = audit.list(entity_type=AuditEntityType.SITE_SETTING)
        assert any(e.action == AuditAction.SETTING_RESET for e in entries)


# ──────────────────────────────────────────────────────────────────────────
# Registry nullable behavior
# ──────────────────────────────────────────────────────────────────────────


class TestRegistryNullable:
    def test_empty_string_env_returns_none_for_nullable(self, monkeypatch):
        monkeypatch.setenv("COMPENDIUM_FINE_BLOCK_THRESHOLD_CENTS", "")
        ss.invalidate_cache()
        assert get_site_setting("fine_block_threshold_cents") is None

    def test_set_none_on_nullable_persists_empty(self, s_session):
        set_site_setting("fine_block_threshold_cents", None, session=s_session)
        s_session.commit()
        ss.invalidate_cache()
        assert get_site_setting("fine_block_threshold_cents") is None

    def test_set_none_on_non_nullable_raises(self, s_session):
        from compendium.services.settings_registry import SettingValidationError

        with pytest.raises(SettingValidationError):
            set_site_setting("library_name", None, session=s_session)
