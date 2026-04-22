"""Web UI tests for notification admin viewer + patron self-service preferences."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.session import get_session
from compendium.domain.enums import HoldStatus, NotificationStatus
from compendium.domain.models import AppUser, Base, Hold, Patron
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.notification_repository import SqlNotificationRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService, hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _sign, generate_token
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)


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
def client(engine, db_session):
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
    with patch("compendium.db.engine.get_settings", return_value=_SETTINGS):
        yield TestClient(app, follow_redirects=False)


def _csrf_pair():
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _SECRET)}"
    return raw, signed


def _make_user(s, role_name, username):
    role = SqlRoleRepository(s).get_by_name(role_name)
    u = AppUser(username=username, password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(s).add(u)
    s.flush()
    u.role = role
    return u


def _login(client, username):
    raw, signed = _csrf_pair()
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": "password", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    return dict(resp.cookies)


def _seed_with_hold_notification(session, patron_card, user=None):
    from compendium.repositories.sql.branch_repository import SqlBranchRepository
    from compendium.repositories.sql.creator_repository import SqlCreatorRepository
    from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
    from compendium.repositories.sql.work_repository import SqlWorkRepository
    from compendium.services.catalog import CatalogService
    from compendium.services.notifications import NotificationService

    kwargs = {
        "library_card_number": patron_card,
        "full_name": "Alice",
        "contact_email": f"{patron_card.lower()}@example.test",
    }
    if user is not None:
        kwargs["user_id"] = user.id
    patron = Patron(**kwargs)
    SqlPatronRepository(session).add(patron)
    session.flush()

    # Unique ISBN per-call to avoid cross-test collision
    isbn = f"978044113{abs(hash(patron_card)) % 10000:04d}"
    with patch(
        "compendium.services.metadata.lookup_isbn",
        return_value={
            "title": "Dune",
            "authors": [{"name": "Frank Herbert"}],
            "publishers": [{"name": "Chilton"}],
            "publish_date": "1965",
            "cover": {},
            "identifiers": {},
        },
    ):
        work, item = CatalogService(
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            creator_repo=SqlCreatorRepository(session),
            branch_repo=SqlBranchRepository(session),
            media_type_repo=SqlMediaTypeRepository(session),
        ).add_from_isbn(isbn)
    session.flush()

    hold = Hold(
        work_id=work.id,
        patron_id=patron.id,
        branch_id=item.branch_id,
        status=HoldStatus.AVAILABLE.value,
        placed_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    SqlHoldRepository(session).add(hold)
    session.flush()

    svc = NotificationService(
        notification_repo=SqlNotificationRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        settings=Settings(database_url="sqlite:///:memory:"),
    )
    n = svc.queue_hold_ready(hold)
    session.commit()
    return patron, n


# ── Admin viewer ─────────────────────────────────────────────────────────────


def test_web_admin_notifications_renders(client, db_session):
    _make_user(db_session, "Librarian", "web_n_adm")
    _seed_with_hold_notification(db_session, "WEB_N_0001")
    db_session.commit()
    cookies = _login(client, "web_n_adm")
    resp = client.get("/ui/admin/notifications", cookies=cookies)
    assert resp.status_code == 200, resp.text
    assert b"Notifications" in resp.content
    assert b"hold_ready" in resp.content


def test_web_admin_notifications_forbidden_readonly(client, db_session):
    _make_user(db_session, "ReadOnly", "web_n_ro")
    db_session.commit()
    cookies = _login(client, "web_n_ro")
    resp = client.get("/ui/admin/notifications", cookies=cookies)
    assert resp.status_code in {302, 303, 403}


def test_web_retry_notification(client, db_session):
    _make_user(db_session, "Librarian", "web_n_retry")
    _, n = _seed_with_hold_notification(db_session, "WEB_N_0002")
    # Mark failed so retry is allowed
    n.status = NotificationStatus.FAILED.value
    n.attempts = 5
    SqlNotificationRepository(db_session).update(n)
    db_session.commit()

    cookies = _login(client, "web_n_retry")
    raw, signed = _csrf_pair()
    resp = client.post(
        f"/ui/admin/notifications/{n.id}/retry",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    db_session.refresh(n)
    assert n.status == NotificationStatus.PENDING.value
    assert n.attempts == 0


# ── /me preferences ───────────────────────────────────────────────────────────


def test_web_my_preferences_renders(client, db_session):
    role = SqlRoleRepository(db_session).get_by_name("Patron")
    u = AppUser(username="web_pref", password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(db_session).add(u)
    db_session.flush()
    u.role = role
    _seed_with_hold_notification(db_session, "WEB_PREF_01", user=u)
    db_session.commit()

    cookies = _login(client, "web_pref")
    resp = client.get("/ui/me/preferences", cookies=cookies)
    assert resp.status_code == 200
    assert b"My Preferences" in resp.content
    assert b"receive_notifications" in resp.content


def test_web_my_preferences_toggle_off_persists(client, db_session):
    role = SqlRoleRepository(db_session).get_by_name("Patron")
    u = AppUser(username="web_pref2", password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(db_session).add(u)
    db_session.flush()
    u.role = role
    patron, _ = _seed_with_hold_notification(db_session, "WEB_PREF_02", user=u)
    db_session.commit()

    cookies = _login(client, "web_pref2")
    raw, signed = _csrf_pair()
    # Omit `receive_notifications` → checkbox unchecked → opt-out.
    resp = client.post(
        "/ui/me/preferences",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    db_session.refresh(patron)
    assert patron.receive_notifications is False


def test_web_my_preferences_toggle_on_persists(client, db_session):
    role = SqlRoleRepository(db_session).get_by_name("Patron")
    u = AppUser(username="web_pref3", password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(db_session).add(u)
    db_session.flush()
    u.role = role
    patron, _ = _seed_with_hold_notification(db_session, "WEB_PREF_03", user=u)
    # Start opted out to verify toggle-on flows through
    patron.receive_notifications = False
    SqlPatronRepository(db_session).update(patron)
    db_session.commit()

    cookies = _login(client, "web_pref3")
    raw, signed = _csrf_pair()
    resp = client.post(
        "/ui/me/preferences",
        data={"csrf_token": raw, "receive_notifications": "on"},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    db_session.refresh(patron)
    assert patron.receive_notifications is True
