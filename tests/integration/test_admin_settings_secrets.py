"""Integration tests for the secrets settings page and encryption layer."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.site_setting_repository import SqlSiteSettingRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services import site_settings as ss
from compendium.services.auth import AuthService, hash_password
from compendium.services.secrets import is_encrypted
from compendium.services.site_settings import get_site_setting, set_site_setting
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from compendium.web.deps import AUTH_COOKIE

_SETTINGS = Settings(database_url="sqlite:///:memory:")
_FERNET_KEY = Fernet.generate_key().decode()
_OTHER_KEY = Fernet.generate_key().decode()


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
def _env_isolation(s_engine, monkeypatch):
    monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
    ss.invalidate_cache()
    for var in ("COMPENDIUM_SECRET_KEY", "COMPENDIUM_SMTP_PASSWORD",
                "COMPENDIUM_TMDB_API_KEY", "COMPENDIUM_GOOGLE_BOOKS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield
    ss.invalidate_cache()


def _make_admin(s) -> tuple[AppUser, str]:
    role = SqlRoleRepository(s).get_by_name("Administrator")
    u = AppUser(username="admin", password_hash=hash_password("pw"), role_id=role.id)
    SqlUserRepository(s).add(u)
    s.commit()
    u.role = role
    token = AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(u)
    return u, token


def _csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _derive_csrf_secret(_SETTINGS.jwt_secret_key))}"
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


# ── GET /admin/system/secrets ─────────────────────────────────────────────


def test_secrets_page_renders_disabled_banner_without_key(client, s_session):
    _, token = _make_admin(s_session)
    resp = client.get(
        "/ui/admin/system/secrets",
        cookies={AUTH_COOKIE: token},
    )
    assert resp.status_code == 200
    assert "COMPENDIUM_SECRET_KEY" in resp.text
    assert "disabled" in resp.text


def test_secrets_page_renders_enabled_without_banner(client, s_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    _, token = _make_admin(s_session)
    resp = client.get(
        "/ui/admin/system/secrets",
        cookies={AUTH_COOKIE: token},
    )
    assert resp.status_code == 200
    assert "Secret storage is disabled" not in resp.text


def test_secrets_page_shows_mismatch_banner(client, s_session, monkeypatch):
    # Write a canary with one key, then switch to another.
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    with Session(s_session.get_bind()) as check_s:
        from compendium.services.secrets import write_canary
        write_canary(check_s)
        check_s.commit()

    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _OTHER_KEY)
    ss.invalidate_cache()

    _, token = _make_admin(s_session)
    resp = client.get(
        "/ui/admin/system/secrets",
        cookies={AUTH_COOKIE: token},
    )
    assert resp.status_code == 200
    assert "Key mismatch" in resp.text


# ── POST /admin/system/secrets ────────────────────────────────────────────


def test_post_secret_encrypts_in_db(client, s_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    _, token = _make_admin(s_session)
    raw_csrf, signed_csrf = _csrf_pair()

    resp = client.post(
        "/ui/admin/system/secrets",
        data={"csrf_token": raw_csrf, "smtp_password": "hunter2"},
        cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed_csrf},
    )
    assert resp.status_code == 303

    # Check the raw DB row is ciphertext.
    with Session(s_session.get_bind()) as s:
        row = SqlSiteSettingRepository(s).get("smtp_password")
    assert row is not None
    assert is_encrypted(row.value), f"Expected encrypted value, got: {row.value!r}"


def test_get_site_setting_decrypts_secret(client, s_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    _, token = _make_admin(s_session)
    raw_csrf, signed_csrf = _csrf_pair()

    client.post(
        "/ui/admin/system/secrets",
        data={"csrf_token": raw_csrf, "smtp_password": "hunter2"},
        cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed_csrf},
    )

    ss.invalidate_cache()
    value = get_site_setting("smtp_password")
    assert value == "hunter2"


def test_env_var_overrides_db_secret(s_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    with Session(s_session.get_bind()) as s:
        set_site_setting("smtp_password", "from-db", session=s)
        s.commit()

    ss.invalidate_cache()
    monkeypatch.setenv("COMPENDIUM_SMTP_PASSWORD", "from-env")
    value = get_site_setting("smtp_password")
    assert value == "from-env"


def test_post_without_key_returns_error(client, s_session):
    _, token = _make_admin(s_session)
    raw_csrf, signed_csrf = _csrf_pair()

    resp = client.post(
        "/ui/admin/system/secrets",
        data={"csrf_token": raw_csrf, "smtp_password": "hunter2"},
        cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed_csrf},
    )
    # Should redirect with an error querystring.
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


def test_audit_log_redacts_secret(client, s_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    _, token = _make_admin(s_session)
    raw_csrf, signed_csrf = _csrf_pair()

    client.post(
        "/ui/admin/system/secrets",
        data={"csrf_token": raw_csrf, "smtp_password": "hunter2"},
        cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed_csrf},
    )

    from compendium.domain.models import AuditLog
    with Session(s_session.get_bind()) as s:
        log = s.query(AuditLog).filter(
            AuditLog.entity_type == "site_setting",
            AuditLog.action == "setting_update",
        ).order_by(AuditLog.id.desc()).first()

    assert log is not None
    assert log.details.get("before") == "***"
    assert log.details.get("after") == "***"
    assert "hunter2" not in str(log.details)
