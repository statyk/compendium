"""API tests for /fines, /patrons/{card}/fines*, /me/fines, /items/{barcode}/lost|damaged|clear-*."""

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
from compendium.domain.enums import FineKind, FineStatus, ItemStatus
from compendium.domain.models import AppUser, Base, Loan, Patron
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
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


def _seed_work_item(s, isbn):
    from compendium.repositories.sql.branch_repository import SqlBranchRepository
    from compendium.repositories.sql.creator_repository import SqlCreatorRepository
    from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
    from compendium.repositories.sql.work_repository import SqlWorkRepository
    from compendium.services.catalog import CatalogService

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
            work_repo=SqlWorkRepository(s),
            item_repo=SqlItemRepository(s),
            creator_repo=SqlCreatorRepository(s),
            branch_repo=SqlBranchRepository(s),
            media_type_repo=SqlMediaTypeRepository(s),
        ).add_from_isbn(isbn)
    s.commit()
    return work, item


def _make_patron(s, card):
    p = Patron(library_card_number=card, full_name="Alice")
    SqlPatronRepository(s).add(p)
    s.flush()
    return p


def _set_policy(s, *, per_day=10, lost_default=None, proc=None):
    pol = SqlLoanPolicyRepository(s).get_default()
    pol.overdue_fine_per_day_cents = per_day
    pol.lost_item_default_cents = lost_default
    pol.lost_item_processing_fee_cents = proc
    s.flush()


def _make_overdue_loan(s, patron, item, days_late=3):
    now = datetime.now(timezone.utc)
    loan = Loan(
        item_id=item.id,
        patron_id=patron.id,
        branch_id=item.branch_id,
        checked_out_at=now - timedelta(days=days_late + 14),
        due_at=now - timedelta(days=days_late),
    )
    SqlLoanRepository(s).add(loan)
    item.status = ItemStatus.CHECKED_OUT.value
    SqlItemRepository(s).update(item)
    s.commit()
    return loan


def test_api_list_patron_fines_requires_fine_manage(client, db_session):
    _, token = _make_user(db_session, "ReadOnly", "api_fine_forbid")
    db_session.commit()
    resp = client.get("/patrons/NOSUCH/fines", headers=_bearer(token))
    assert resp.status_code == 403


def test_api_assess_manual_and_list(client, db_session):
    _make_patron(db_session, "API_F0001")
    _, token = _make_user(db_session, "Librarian", "api_fine_assess")
    db_session.commit()

    resp = client.post(
        "/fines",
        json={
            "patron_card": "API_F0001",
            "kind": "other",
            "amount_cents": 500,
            "note": "card replacement",
        },
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["amount_cents"] == 500
    assert body["kind"] == "other"

    resp2 = client.get("/patrons/API_F0001/fines", headers=_bearer(token))
    assert resp2.status_code == 200
    assert len(resp2.json()) == 1


def test_api_pay_fine(client, db_session):
    p = _make_patron(db_session, "API_F0002")
    _, token = _make_user(db_session, "Librarian", "api_fine_pay")
    db_session.commit()

    client.post(
        "/fines",
        json={"patron_card": "API_F0002", "kind": "other", "amount_cents": 200, "note": "x"},
        headers=_bearer(token),
    )
    fine_id = client.get("/patrons/API_F0002/fines", headers=_bearer(token)).json()[0]["id"]
    resp = client.post(f"/fines/{fine_id}/pay", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.json()["status"] == FineStatus.PAID.value


def test_api_waive_requires_note(client, db_session):
    _make_patron(db_session, "API_F0003")
    _, token = _make_user(db_session, "Librarian", "api_fine_waive")
    db_session.commit()
    client.post(
        "/fines",
        json={"patron_card": "API_F0003", "kind": "other", "amount_cents": 100, "note": "x"},
        headers=_bearer(token),
    )
    fine_id = client.get("/patrons/API_F0003/fines", headers=_bearer(token)).json()[0]["id"]
    resp_bad = client.post(
        f"/fines/{fine_id}/waive", json={"note": ""}, headers=_bearer(token)
    )
    assert resp_bad.status_code == 422
    resp_ok = client.post(
        f"/fines/{fine_id}/waive", json={"note": "goodwill"}, headers=_bearer(token)
    )
    assert resp_ok.status_code == 200
    assert resp_ok.json()["status"] == FineStatus.WAIVED.value


def test_api_assess_overdue_scoped_to_patron(client, db_session):
    _, item = _seed_work_item(db_session, "9780441099001")
    p = _make_patron(db_session, "API_F0004")
    _set_policy(db_session, per_day=50)
    _make_overdue_loan(db_session, p, item, days_late=3)
    _, token = _make_user(db_session, "Librarian", "api_fine_overdue")
    db_session.commit()

    resp = client.post(
        "/patrons/API_F0004/fines/assess-overdue",
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 1


def test_api_me_fines_self_service(client, db_session):
    role = SqlRoleRepository(db_session).get_by_name("Patron")
    user = AppUser(username="api_me_fines", password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(db_session).add(user)
    db_session.flush()
    user.role = role
    patron = Patron(library_card_number="API_ME_F01", full_name="Me", user_id=user.id)
    SqlPatronRepository(db_session).add(patron)
    db_session.flush()

    token = AuthService(
        user_repo=SqlUserRepository(db_session),
        role_repo=SqlRoleRepository(db_session),
        settings=_SETTINGS,
    ).issue_token(user)

    # Librarian assesses a manual fine against this patron
    _, lib_token = _make_user(db_session, "Librarian", "api_me_fines_lib")
    db_session.commit()
    client.post(
        "/fines",
        json={"patron_card": "API_ME_F01", "kind": "other", "amount_cents": 150, "note": "x"},
        headers=_bearer(lib_token),
    )
    resp = client.get("/me/fines", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 1
    assert data[0]["amount_cents"] == 150


def test_api_declare_lost(client, db_session):
    _, item = _seed_work_item(db_session, "9780441099002")
    p = _make_patron(db_session, "API_F0005")
    _set_policy(db_session, lost_default=1500, proc=250)
    _make_overdue_loan(db_session, p, item, days_late=0)
    _, token = _make_user(db_session, "Librarian", "api_fine_lost")
    db_session.commit()

    resp = client.post(
        f"/items/{item.barcode}/lost",
        json={},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == ItemStatus.LOST.value
    fines = client.get("/patrons/API_F0005/fines", headers=_bearer(token)).json()
    assert len(fines) == 2  # lost + processing
    kinds = {f["kind"] for f in fines}
    assert FineKind.LOST.value in kinds
    assert FineKind.PROCESSING.value in kinds


def test_api_mark_damaged_requires_note(client, db_session):
    _, item = _seed_work_item(db_session, "9780441099003")
    p = _make_patron(db_session, "API_F0006")
    _make_overdue_loan(db_session, p, item, days_late=0)
    _, token = _make_user(db_session, "Librarian", "api_fine_dmg")
    db_session.commit()

    bad = client.post(
        f"/items/{item.barcode}/damaged",
        json={"amount_cents": 500, "note": ""},
        headers=_bearer(token),
    )
    assert bad.status_code == 422
    ok = client.post(
        f"/items/{item.barcode}/damaged",
        json={"amount_cents": 500, "note": "spine torn"},
        headers=_bearer(token),
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == ItemStatus.DAMAGED.value


def test_api_clear_damage_and_clear_lost(client, db_session):
    _, item = _seed_work_item(db_session, "9780441099004")
    p = _make_patron(db_session, "API_F0007")
    _set_policy(db_session, lost_default=1500)
    _make_overdue_loan(db_session, p, item, days_late=0)
    _, token = _make_user(db_session, "Librarian", "api_fine_clear")
    db_session.commit()

    client.post(
        f"/items/{item.barcode}/damaged",
        json={"amount_cents": 500, "note": "x"},
        headers=_bearer(token),
    )
    r1 = client.post(f"/items/{item.barcode}/clear-damage", headers=_bearer(token))
    assert r1.status_code == 200
    assert r1.json()["status"] == ItemStatus.AVAILABLE.value

    # Now declare lost and clear
    # Need another loan since declare_lost closes the loan.
    _make_overdue_loan(db_session, p, item, days_late=0)
    client.post(f"/items/{item.barcode}/lost", json={}, headers=_bearer(token))
    r2 = client.post(f"/items/{item.barcode}/clear-lost", headers=_bearer(token))
    assert r2.status_code == 200
    assert r2.json()["status"] == ItemStatus.AVAILABLE.value


def test_api_unknown_patron_on_list_fines(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_fine_404")
    db_session.commit()
    resp = client.get("/patrons/NOSUCH/fines", headers=_bearer(token))
    assert resp.status_code == 404


def test_api_fines_unauthenticated(client):
    resp = client.post("/fines", json={})
    assert resp.status_code == 401
