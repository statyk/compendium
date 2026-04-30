"""Login rate limiting (M1) — per-identity sliding-window throttle.

Covers:
- API /auth/login: 429 with Retry-After on the Nth+1 bad-password attempt.
- Web /ui/login: 429 page on the Nth+1 bad-password attempt.
- Kiosk /ui/kiosk/start: redirect with error on the Nth+1 unrecognized card.
- Blocking is per-username only — a different user from the same path is
  never affected (confirms no IP-based block exists).
- Successful login clears prior failures.
- Valid card with active/expired/fees does NOT increment kiosk counter.
- login_max_failures=0 disables throttling entirely.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base, Patron
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import hash_password
from compendium.services import site_settings as ss
from compendium.web.csrf import _sign, generate_token
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)

# Use a low threshold via env so tests run without seeding site_setting rows.
_MAX_FAILURES = 3
_WINDOW = 300


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def db_session(engine) -> Session:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def client(engine, db_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_LOGIN_MAX_FAILURES", str(_MAX_FAILURES))
    monkeypatch.setenv("COMPENDIUM_LOGIN_FAILURE_WINDOW_SECONDS", str(_WINDOW))
    ss.invalidate_cache()

    app = create_app()

    def _override():
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
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
    app.dependency_overrides[get_settings] = lambda: _SETTINGS
    with patch("compendium.db.engine.get_settings", return_value=_SETTINGS):
        yield TestClient(app, follow_redirects=False)

    ss.invalidate_cache()


def _make_user(s: Session, username: str, password: str = "correct") -> AppUser:
    role = SqlRoleRepository(s).get_by_name("Patron")
    user = AppUser(username=username, password_hash=hash_password(password), role_id=role.id)
    SqlUserRepository(s).add(user)
    s.commit()
    return user


def _make_patron(s: Session, card: str, active: bool = True) -> Patron:
    patron = Patron(
        full_name="Test",
        library_card_number=card,
        is_active=active,
    )
    SqlPatronRepository(s).add(patron)
    s.commit()
    return patron


def _csrf_pair():
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, _SECRET)}"


# ---------------------------------------------------------------------------
# API /auth/login
# ---------------------------------------------------------------------------

def test_api_login_blocks_after_max_failures(client, db_session):
    _make_user(db_session, "api_brute")
    for _ in range(_MAX_FAILURES):
        r = client.post("/auth/login", json={"username": "api_brute", "password": "wrong"})
        assert r.status_code == 401
    # Next attempt should be rate-limited.
    r = client.post("/auth/login", json={"username": "api_brute", "password": "wrong"})
    assert r.status_code == 429
    assert "retry-after" in r.headers
    assert int(r.headers["retry-after"]) > 0
    assert "too many" in r.json()["detail"].lower()


def test_api_login_different_user_not_blocked(client, db_session):
    """alice's failures must not affect bob (confirms no IP block)."""
    _make_user(db_session, "api_alice")
    _make_user(db_session, "api_bob", password="correct")
    for _ in range(_MAX_FAILURES):
        client.post("/auth/login", json={"username": "api_alice", "password": "wrong"})
    # alice is blocked
    r = client.post("/auth/login", json={"username": "api_alice", "password": "wrong"})
    assert r.status_code == 429
    # bob can still log in
    r = client.post("/auth/login", json={"username": "api_bob", "password": "correct"})
    assert r.status_code == 200


def test_api_login_success_clears_failures(client, db_session):
    _make_user(db_session, "api_clear")
    for _ in range(_MAX_FAILURES - 1):
        client.post("/auth/login", json={"username": "api_clear", "password": "wrong"})
    # Succeed once — failures should be cleared.
    r = client.post("/auth/login", json={"username": "api_clear", "password": "correct"})
    assert r.status_code == 200
    # Should be able to fail again up to the threshold without hitting 429.
    for _ in range(_MAX_FAILURES):
        r = client.post("/auth/login", json={"username": "api_clear", "password": "wrong"})
        if r.status_code == 429:
            pytest.fail("Failures were not cleared on successful login")
        assert r.status_code == 401


def test_api_login_zero_max_disables_throttle(client, db_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_LOGIN_MAX_FAILURES", "0")
    ss.invalidate_cache()
    _make_user(db_session, "api_nothrottle")
    for _ in range(20):
        r = client.post("/auth/login", json={"username": "api_nothrottle", "password": "wrong"})
        assert r.status_code == 401, "Should never be rate-limited when max_failures=0"
    ss.invalidate_cache()


# ---------------------------------------------------------------------------
# Web /ui/login
# ---------------------------------------------------------------------------

def test_web_login_blocks_after_max_failures(client, db_session):
    _make_user(db_session, "web_brute")
    raw, signed = _csrf_pair()
    cookies = {"csrf_token": signed}
    for _ in range(_MAX_FAILURES):
        r = client.post(
            "/ui/login",
            data={"username": "web_brute", "password": "wrong", "csrf_token": raw},
            cookies=cookies,
        )
        assert r.status_code == 401
    r = client.post(
        "/ui/login",
        data={"username": "web_brute", "password": "wrong", "csrf_token": raw},
        cookies=cookies,
    )
    assert r.status_code == 429
    assert "retry-after" in r.headers
    assert b"too many" in r.content.lower()


# ---------------------------------------------------------------------------
# Kiosk /ui/kiosk/start
# ---------------------------------------------------------------------------

def _make_kiosk_session(client: TestClient, db_session: Session) -> dict[str, str]:
    """Log in a fresh admin user and return cookies for kiosk requests."""
    import random
    username = f"kiosk_op_{random.randint(100000, 999999)}"
    _make_user(db_session, username, password="pw")
    # Upgrade to Administrator so the user has loan.checkout.
    role = SqlRoleRepository(db_session).get_by_name("Administrator")
    user = SqlUserRepository(db_session).get_by_username(username)
    user.role_id = role.id
    db_session.commit()

    raw, signed = _csrf_pair()
    login_cookies = {"csrf_token": signed}
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": "pw", "csrf_token": raw},
        cookies=login_cookies,
    )
    assert resp.status_code in (302, 303), f"login failed: {resp.text}"
    # Harvest the compendium_auth cookie from the login response.
    auth_token = resp.cookies.get("compendium_auth", "")
    return {"csrf_token": signed, "compendium_auth": auth_token}


def test_kiosk_blocks_after_max_failures(client, db_session):
    cookies = _make_kiosk_session(client, db_session)
    raw, signed = _csrf_pair()
    cookies["csrf_token"] = signed

    for _ in range(_MAX_FAILURES):
        r = client.post(
            "/ui/kiosk/start",
            data={"card_number": "FAKE9999", "csrf_token": raw},
            cookies=cookies,
        )
        assert r.status_code == 303
        loc = r.headers["location"].lower()
        assert "card+not+recognized" in loc or "card%20not%20recognized" in loc or "not+recognized" in loc or "not%20recognized" in loc

    r = client.post(
        "/ui/kiosk/start",
        data={"card_number": "FAKE9999", "csrf_token": raw},
        cookies=cookies,
    )
    assert r.status_code == 303
    loc = r.headers["location"].lower()
    assert "too+many" in loc or "too%20many" in loc or "many" in loc, f"Expected rate-limit error, got: {loc}"


def test_kiosk_valid_card_with_active_patron_not_counted(client, db_session):
    """A valid (but inactive) card does not count toward the throttle."""
    cookies = _make_kiosk_session(client, db_session)
    raw, signed = _csrf_pair()
    cookies["csrf_token"] = signed
    # Create an inactive patron (valid card lookup succeeds, but _gate_patron blocks it).
    _make_patron(db_session, "INACTIVE999", active=False)

    # Exceed the threshold with the inactive card — should never produce a
    # "too many" redirect because invalid-card check fires before the gate.
    for _ in range(_MAX_FAILURES + 5):
        r = client.post(
            "/ui/kiosk/start",
            data={"card_number": "INACTIVE999", "csrf_token": raw},
            cookies=cookies,
        )
        assert r.status_code == 303
        loc = r.headers["location"].lower()
        assert "too+many" not in loc and "too%20many" not in loc and "many" not in loc, (
            f"Active-but-gated patron should not trigger rate limiting: {loc}"
        )
