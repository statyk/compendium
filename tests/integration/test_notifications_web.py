"""Web UI tests for notification admin viewer + patron self-service preferences."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from compendium.domain.enums import HoldStatus, NotificationStatus
from compendium.domain.models import Hold, Patron
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.notification_repository import SqlNotificationRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from tests.helpers import csrf_pair, make_client, make_engine, make_user, session_for, std_settings


@pytest.fixture(scope="module")
def engine():
    return make_engine()


@pytest.fixture
def db_session(engine) -> Session:
    yield from session_for(engine)


@pytest.fixture
def client(engine, db_session):
    with make_client(engine) as c:
        yield c


def _login(client, username):
    raw, signed = csrf_pair()
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
        settings=std_settings(),
    )
    n = svc.queue_hold_ready(hold)
    session.commit()
    return patron, n


# ── Admin viewer ─────────────────────────────────────────────────────────────


def test_web_admin_notifications_renders(client, db_session):
    make_user(db_session, "web_n_adm", "Librarian")
    _seed_with_hold_notification(db_session, "WEB_N_0001")
    db_session.commit()
    cookies = _login(client, "web_n_adm")
    resp = client.get("/ui/admin/notifications", cookies=cookies)
    assert resp.status_code == 200, resp.text
    assert b"Notifications" in resp.content
    assert b"hold_ready" in resp.content


def test_notification_log_links_to_smtp_settings(client, db_session):
    make_user(db_session, "web_n_smtp_link", "Administrator")
    db_session.commit()
    cookies = _login(client, "web_n_smtp_link")
    resp = client.get("/ui/admin/notifications", cookies=cookies)
    assert resp.status_code == 200, resp.text
    # The global nav already links /ui/admin/system/smtp, so assert on the
    # new page-body sentence (not just the bare URL) to avoid a false
    # positive from the nav.
    assert "SMTP settings" in resp.text
    assert '/ui/admin/system/smtp">SMTP settings</a>' in resp.text


def test_notification_log_hides_smtp_link_for_librarian(client, db_session):
    # Librarian has notification.manage (can view the log) but not
    # system.manage, so the SMTP cross-link — gated on system.manage — must
    # not appear.
    make_user(db_session, "web_n_lib_smtp", "Librarian")
    db_session.commit()
    cookies = _login(client, "web_n_lib_smtp")
    resp = client.get("/ui/admin/notifications", cookies=cookies)
    assert resp.status_code == 200, resp.text
    assert "SMTP settings" not in resp.text


def test_web_admin_notifications_forbidden_readonly(client, db_session):
    make_user(db_session, "web_n_ro", "ReadOnly")
    db_session.commit()
    cookies = _login(client, "web_n_ro")
    resp = client.get("/ui/admin/notifications", cookies=cookies)
    assert resp.status_code in {302, 303, 403}


def test_web_retry_notification(client, db_session):
    make_user(db_session, "web_n_retry", "Librarian")
    _, n = _seed_with_hold_notification(db_session, "WEB_N_0002")
    n.status = NotificationStatus.FAILED.value
    n.attempts = 5
    SqlNotificationRepository(db_session).update(n)
    db_session.commit()

    cookies = _login(client, "web_n_retry")
    raw, signed = csrf_pair()
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
    u, _ = make_user(db_session, "web_pref", "Patron")
    _seed_with_hold_notification(db_session, "WEB_PREF_01", user=u)
    db_session.commit()

    cookies = _login(client, "web_pref")
    resp = client.get("/ui/me/preferences", cookies=cookies)
    assert resp.status_code == 200
    assert b"My Preferences" in resp.content
    assert b"receive_notifications" in resp.content


def test_web_my_preferences_toggle_off_persists(client, db_session):
    u, _ = make_user(db_session, "web_pref2", "Patron")
    patron, _ = _seed_with_hold_notification(db_session, "WEB_PREF_02", user=u)
    db_session.commit()

    cookies = _login(client, "web_pref2")
    raw, signed = csrf_pair()
    resp = client.post(
        "/ui/me/preferences",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    db_session.refresh(patron)
    assert patron.receive_notifications is False


def test_web_my_preferences_toggle_on_persists(client, db_session):
    u, _ = make_user(db_session, "web_pref3", "Patron")
    patron, _ = _seed_with_hold_notification(db_session, "WEB_PREF_03", user=u)
    patron.receive_notifications = False
    SqlPatronRepository(db_session).update(patron)
    db_session.commit()

    cookies = _login(client, "web_pref3")
    raw, signed = csrf_pair()
    resp = client.post(
        "/ui/me/preferences",
        data={"csrf_token": raw, "receive_notifications": "on"},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    db_session.refresh(patron)
    assert patron.receive_notifications is True
