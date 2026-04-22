"""API tests for GET /notifications + POST /notifications/{id}/retry."""

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


def _make_user(s, role_name, username):
    role = SqlRoleRepository(s).get_by_name(role_name)
    u = AppUser(username=username, password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(s).add(u)
    s.flush()
    u.role = role
    token = AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(u)
    return u, token


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _queue_hold_ready(session, patron_card):
    from compendium.repositories.sql.branch_repository import SqlBranchRepository
    from compendium.repositories.sql.creator_repository import SqlCreatorRepository
    from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
    from compendium.repositories.sql.work_repository import SqlWorkRepository
    from compendium.services.catalog import CatalogService
    from compendium.services.notifications import NotificationService

    patron = Patron(
        library_card_number=patron_card,
        full_name="Alice",
        contact_email=f"{patron_card.lower()}@example.test",
    )
    SqlPatronRepository(session).add(patron)
    session.flush()

    isbn = f"978044114{abs(hash(patron_card)) % 10000:04d}"
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
    return n


def test_api_list_notifications_requires_manage(client, db_session):
    _, token = _make_user(db_session, "ReadOnly", "api_n_ro")
    db_session.commit()
    resp = client.get("/notifications", headers=_bearer(token))
    assert resp.status_code == 403


def test_api_list_notifications_ok(client, db_session):
    _queue_hold_ready(db_session, "API_N_0001")
    _, token = _make_user(db_session, "Librarian", "api_n_list")
    db_session.commit()
    resp = client.get("/notifications", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) >= 1
    assert any(r["template_key"] == "hold_ready" for r in rows)


def test_api_list_notifications_status_filter(client, db_session):
    _queue_hold_ready(db_session, "API_N_0002")
    _, token = _make_user(db_session, "Librarian", "api_n_filter")
    db_session.commit()
    resp = client.get("/notifications?status=pending", headers=_bearer(token))
    assert resp.status_code == 200
    assert all(r["status"] == "pending" for r in resp.json())


def test_api_retry_pending(client, db_session):
    n = _queue_hold_ready(db_session, "API_N_0003")
    # Flip to failed so retry is the meaningful transition.
    n.status = NotificationStatus.FAILED.value
    n.attempts = 5
    SqlNotificationRepository(db_session).update(n)
    db_session.commit()

    _, token = _make_user(db_session, "Librarian", "api_n_retry")
    db_session.commit()
    resp = client.post(f"/notifications/{n.id}/retry", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"


def test_api_retry_unknown_id_404(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_n_404")
    db_session.commit()
    resp = client.post("/notifications/99999/retry", headers=_bearer(token))
    assert resp.status_code == 404


def test_api_retry_sent_row_422(client, db_session):
    n = _queue_hold_ready(db_session, "API_N_0004")
    n.status = NotificationStatus.SENT.value
    SqlNotificationRepository(db_session).update(n)
    db_session.commit()
    _, token = _make_user(db_session, "Librarian", "api_n_sent")
    db_session.commit()
    resp = client.post(f"/notifications/{n.id}/retry", headers=_bearer(token))
    assert resp.status_code == 422


def test_api_notifications_unauthenticated(client):
    resp = client.get("/notifications")
    assert resp.status_code == 401
