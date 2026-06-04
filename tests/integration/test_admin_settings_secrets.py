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


def test_secrets_page_redirects_to_metadata(client, s_session):
    # The old /secrets URL now permanently redirects to Metadata Sources.
    _, token = _make_admin(s_session)
    resp = client.get(
        "/ui/admin/system/secrets",
        cookies={AUTH_COOKIE: token},
        follow_redirects=False,
    )
    assert resp.status_code == 301
    assert "/ui/admin/system/metadata" in resp.headers["location"]


def test_secrets_banner_disabled_shown_on_metadata_page(client, s_session):
    # When secret key is not configured the banner appears on the metadata page.
    _, token = _make_admin(s_session)
    resp = client.get(
        "/ui/admin/system/metadata",
        cookies={AUTH_COOKIE: token},
    )
    assert resp.status_code == 200
    assert "COMPENDIUM_SECRET_KEY" in resp.text
    assert "disabled" in resp.text


def test_secrets_banner_absent_when_key_configured(client, s_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    _, token = _make_admin(s_session)
    resp = client.get(
        "/ui/admin/system/metadata",
        cookies={AUTH_COOKIE: token},
    )
    assert resp.status_code == 200
    assert "Secret storage is disabled" not in resp.text


def test_secrets_mismatch_banner_shown_on_metadata_page(client, s_session, monkeypatch):
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
        "/ui/admin/system/metadata",
        cookies={AUTH_COOKIE: token},
    )
    assert resp.status_code == 200
    assert "Key mismatch" in resp.text


# ── Inline secrets on the metadata page ───────────────────────────────────


def test_metadata_page_renders_inline_key_no_separate_section(client, s_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    _, token = _make_admin(s_session)
    resp = client.get("/ui/admin/system/metadata", cookies={AUTH_COOKIE: token})
    assert resp.status_code == 200
    assert 'name="google_books_api_key"' in resp.text
    assert "<h3>API Keys</h3>" not in resp.text
    assert 'action="/ui/admin/system/secrets"' not in resp.text


def test_metadata_page_post_saves_secret_inline(client, s_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    _, token = _make_admin(s_session)
    raw_csrf, signed_csrf = _csrf_pair()
    resp = client.post(
        "/ui/admin/system/metadata",
        data={"csrf_token": raw_csrf, "tmdb_api_key": "tmdb-secret-123"},
        cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed_csrf},
    )
    assert resp.status_code == 303
    with Session(s_session.get_bind()) as s:
        row = SqlSiteSettingRepository(s).get("tmdb_api_key")
    assert row is not None
    assert is_encrypted(row.value)


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


# ── GB API key pre-save validation ───────────────────────────────────────────


def test_invalid_gb_key_blocks_save_with_banner(client, s_session, monkeypatch):
    """Posting a bad GB key redirects with an error; the key is NOT saved."""
    from unittest.mock import patch
    from compendium.services.metadata import KeyValidationResult

    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    _, token = _make_admin(s_session)
    raw_csrf, signed_csrf = _csrf_pair()

    bad_result = KeyValidationResult(ok=False, reason="API key not valid")

    with patch(
        "compendium.web.routes.admin_settings._SECRET_VALIDATORS",
        {"google_books_api_key": lambda _: bad_result},
    ):
        resp = client.post(
            "/ui/admin/system/secrets",
            data={"csrf_token": raw_csrf, "google_books_api_key": "bad-key"},
            cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed_csrf},
        )

    # Should redirect with an error, not save.
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]

    # The key must NOT have been saved.
    from sqlalchemy.orm import Session
    with Session(s_session.get_bind()) as s:
        row = SqlSiteSettingRepository(s).get("google_books_api_key")
    assert row is None


def test_override_checkbox_forces_save_despite_validation_failure(client, s_session, monkeypatch):
    """Checking override_validation_google_books_api_key bypasses the validator and saves."""
    from unittest.mock import patch
    from compendium.services.metadata import KeyValidationResult

    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    _, token = _make_admin(s_session)
    raw_csrf, signed_csrf = _csrf_pair()

    bad_result = KeyValidationResult(ok=False, reason="API key not valid")

    with patch(
        "compendium.web.routes.admin_settings._SECRET_VALIDATORS",
        {"google_books_api_key": lambda _: bad_result},
    ):
        resp = client.post(
            "/ui/admin/system/secrets",
            data={
                "csrf_token": raw_csrf,
                "google_books_api_key": "forced-key",
                "override_validation_google_books_api_key": "1",
            },
            cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed_csrf},
        )

    # Should redirect on successful save.
    assert resp.status_code == 303

    from sqlalchemy.orm import Session
    with Session(s_session.get_bind()) as s:
        row = SqlSiteSettingRepository(s).get("google_books_api_key")
    assert row is not None


def test_valid_gb_key_saves_cleanly(client, s_session, monkeypatch):
    """A key that passes validation is saved without any banner."""
    from unittest.mock import patch
    from compendium.services.metadata import KeyValidationResult

    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    _, token = _make_admin(s_session)
    raw_csrf, signed_csrf = _csrf_pair()

    good_result = KeyValidationResult(ok=True)

    with patch(
        "compendium.web.routes.admin_settings._SECRET_VALIDATORS",
        {"google_books_api_key": lambda _: good_result},
    ):
        resp = client.post(
            "/ui/admin/system/secrets",
            data={"csrf_token": raw_csrf, "google_books_api_key": "good-key"},
            cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed_csrf},
        )

    assert resp.status_code == 303
    assert "validation" not in resp.headers.get("location", "")


def test_quota_exhausted_key_saves_with_warning(client, s_session, monkeypatch):
    """A quota-exhausted key (ok=True, warning set) is saved but shows a warning in redirect."""
    from unittest.mock import patch
    from compendium.services.metadata import KeyValidationResult

    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    _, token = _make_admin(s_session)
    raw_csrf, signed_csrf = _csrf_pair()

    quota_result = KeyValidationResult(ok=True, warning="Quota exhausted; key is valid but temporarily blocked.")

    with patch(
        "compendium.web.routes.admin_settings._SECRET_VALIDATORS",
        {"google_books_api_key": lambda _: quota_result},
    ):
        resp = client.post(
            "/ui/admin/system/secrets",
            data={"csrf_token": raw_csrf, "google_books_api_key": "quota-key"},
            cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed_csrf},
        )

    # Should redirect (save succeeded) with error/warning in query string.
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]

    from sqlalchemy.orm import Session
    with Session(s_session.get_bind()) as s:
        row = SqlSiteSettingRepository(s).get("google_books_api_key")
    assert row is not None


def test_missing_secret_key_env_surfaces_before_validation(client, s_session):
    """When COMPENDIUM_SECRET_KEY is not set, save fails with a storage error (not a validation error)."""
    from unittest.mock import patch
    from compendium.services.metadata import KeyValidationResult

    # No COMPENDIUM_SECRET_KEY in env (already ensured by _env_isolation autouse).
    _, token = _make_admin(s_session)
    raw_csrf, signed_csrf = _csrf_pair()

    good_result = KeyValidationResult(ok=True)

    with patch(
        "compendium.web.routes.admin_settings._SECRET_VALIDATORS",
        {"google_books_api_key": lambda _: good_result},
    ):
        resp = client.post(
            "/ui/admin/system/secrets",
            data={"csrf_token": raw_csrf, "google_books_api_key": "any-key"},
            cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed_csrf},
        )

    # Save is blocked by the missing encryption key, not a validation failure.
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert "validation" not in resp.headers["location"]


def test_smtp_page_renders_inline_password_no_separate_section(client, s_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    _, token = _make_admin(s_session)
    resp = client.get("/ui/admin/system/smtp", cookies={AUTH_COOKIE: token})
    assert resp.status_code == 200
    assert 'name="smtp_password"' in resp.text
    assert "<h3>API Keys</h3>" not in resp.text
    assert 'action="/ui/admin/system/secrets"' not in resp.text


def test_metadata_page_shows_active_source_badge_when_no_key(client, s_session):
    # No google_books_api_key set (env-isolation clears it) → effective source is OL.
    _, token = _make_admin(s_session)
    resp = client.get("/ui/admin/system/metadata", cookies={AUTH_COOKIE: token})
    assert resp.status_code == 200
    assert "Currently using:" in resp.text
    assert "Open Library" in resp.text
    # The googlebooks option is relabeled to signal the key requirement.
    assert "Google Books (requires API key)" in resp.text


def test_metadata_page_clear_removes_secret_inline(client, s_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SECRET_KEY", _FERNET_KEY)
    _, token = _make_admin(s_session)
    # Seed a stored secret.
    raw1, signed1 = _csrf_pair()
    client.post(
        "/ui/admin/system/metadata",
        data={"csrf_token": raw1, "tmdb_api_key": "tmdb-to-clear"},
        cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed1},
    )
    with Session(s_session.get_bind()) as s:
        assert SqlSiteSettingRepository(s).get("tmdb_api_key") is not None
    # Clear it through the page form's clear checkbox.
    raw2, signed2 = _csrf_pair()
    resp = client.post(
        "/ui/admin/system/metadata",
        data={"csrf_token": raw2, "clear": "tmdb_api_key"},
        cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed2},
    )
    assert resp.status_code == 303
    with Session(s_session.get_bind()) as s:
        assert SqlSiteSettingRepository(s).get("tmdb_api_key") is None
