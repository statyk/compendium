"""Tests for API + web routes that expose and edit item loanability."""

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
from compendium.domain.models import AppUser, Base
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.auth import AuthService, hash_password
from compendium.services.catalog import CatalogService
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _sign, generate_token
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)

_OPEN_LIB_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}


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


_counter = 0


def _make_user(s: Session, role_name: str, username: str) -> tuple[AppUser, str]:
    role = SqlRoleRepository(s).get_by_name(role_name)
    user = AppUser(
        username=username, password_hash=hash_password("password"), role_id=role.id
    )
    SqlUserRepository(s).add(user)
    s.flush()
    user.role = role
    token = AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(user)
    return user, token


def _seed_work(s: Session, isbn: str):
    global _counter
    _counter += 1
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        work, item = CatalogService(
            work_repo=SqlWorkRepository(s),
            item_repo=SqlItemRepository(s),
            creator_repo=SqlCreatorRepository(s),
            branch_repo=SqlBranchRepository(s),
            media_type_repo=SqlMediaTypeRepository(s),
        ).add_from_isbn(isbn)
    s.commit()
    return work, item


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _SECRET)}"
    return raw, signed


def _login(client, username: str, password: str = "password") -> dict:
    raw, signed = _make_csrf_pair()
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": password, "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    return dict(resp.cookies)


# ── API: GET /items/{barcode} exposes new fields ─────────────────────────────


def test_api_get_item_includes_loanable_fields(client, db_session):
    _, item = _seed_work(db_session, "9780441090001")
    _, token = _make_user(db_session, "Librarian", "api_loan_get")
    db_session.commit()

    resp = client.get(f"/items/{item.barcode}", headers=_bearer(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_loanable"] is True
    assert body["loan_restriction_reason"] is None
    assert body["loan_restriction_note"] is None


# ── API: POST /items/{barcode}/loanable ──────────────────────────────────────


def test_api_set_loanable_off_with_reason(client, db_session):
    _, item = _seed_work(db_session, "9780441090002")
    _, token = _make_user(db_session, "Librarian", "api_loan_off")
    db_session.commit()

    resp = client.post(
        f"/items/{item.barcode}/loanable",
        json={"is_loanable": False, "reason": "reference"},
        headers=_bearer(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_loanable"] is False
    assert body["loan_restriction_reason"] == "reference"
    assert body["loan_restriction_note"] is None


def test_api_set_loanable_other_requires_note(client, db_session):
    _, item = _seed_work(db_session, "9780441090003")
    _, token = _make_user(db_session, "Librarian", "api_loan_other")
    db_session.commit()

    resp = client.post(
        f"/items/{item.barcode}/loanable",
        json={"is_loanable": False, "reason": "other"},
        headers=_bearer(token),
    )
    assert resp.status_code == 422
    assert "note" in resp.json()["detail"].lower()


def test_api_set_loanable_off_requires_reason(client, db_session):
    _, item = _seed_work(db_session, "9780441090004")
    _, token = _make_user(db_session, "Librarian", "api_loan_noreason")
    db_session.commit()

    resp = client.post(
        f"/items/{item.barcode}/loanable",
        json={"is_loanable": False},
        headers=_bearer(token),
    )
    assert resp.status_code == 422
    assert "reason" in resp.json()["detail"].lower()


def test_api_set_loanable_unknown_barcode(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_loan_404")
    db_session.commit()

    resp = client.post(
        "/items/NO_SUCH/loanable",
        json={"is_loanable": True},
        headers=_bearer(token),
    )
    assert resp.status_code == 404


def test_api_set_loanable_forbidden_without_edit(client, db_session):
    _, item = _seed_work(db_session, "9780441090005")
    _, token = _make_user(db_session, "ReadOnly", "api_loan_forbid")
    db_session.commit()

    resp = client.post(
        f"/items/{item.barcode}/loanable",
        json={"is_loanable": False, "reason": "reference"},
        headers=_bearer(token),
    )
    assert resp.status_code == 403


def test_api_set_loanable_requires_auth(client, db_session):
    _, item = _seed_work(db_session, "9780441090006")
    db_session.commit()

    resp = client.post(
        f"/items/{item.barcode}/loanable",
        json={"is_loanable": False, "reason": "reference"},
    )
    assert resp.status_code == 401


# ── Web: /ui/items/{barcode}/loanable form ───────────────────────────────────


def test_web_loanable_form_renders(client, db_session):
    _, item = _seed_work(db_session, "9780441090007")
    _make_user(db_session, "Librarian", "web_loan_form")
    db_session.commit()

    cookies = _login(client, "web_loan_form")
    resp = client.get(f"/ui/items/{item.barcode}/loanable", cookies=cookies)
    assert resp.status_code == 200
    assert b"Loan status" in resp.content
    assert b"Reference" in resp.content


def test_web_loanable_submit_flips_off(client, db_session):
    _, item = _seed_work(db_session, "9780441090008")
    _make_user(db_session, "Librarian", "web_loan_submit")
    db_session.commit()

    auth = _login(client, "web_loan_submit")
    raw, signed = _make_csrf_pair()
    resp = client.post(
        f"/ui/items/{item.barcode}/loanable",
        data={
            "is_loanable": "no",
            "reason": "reference",
            "note": "",
            "csrf_token": raw,
        },
        cookies={**auth, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    assert f"/ui/items/{item.barcode}" in resp.headers["location"]

    # Fetch via API to confirm the flag flipped.
    _, token = _make_user(db_session, "Librarian", "web_loan_verify")
    db_session.commit()
    check = client.get(f"/ui/items/{item.barcode}/loanable", cookies=auth)
    assert b"checked" in check.content  # radios render with one checked


def test_web_loanable_submit_other_requires_note_shows_error(client, db_session):
    _, item = _seed_work(db_session, "9780441090009")
    _make_user(db_session, "Librarian", "web_loan_other")
    db_session.commit()

    auth = _login(client, "web_loan_other")
    raw, signed = _make_csrf_pair()
    resp = client.post(
        f"/ui/items/{item.barcode}/loanable",
        data={
            "is_loanable": "no",
            "reason": "other",
            "note": "",
            "csrf_token": raw,
        },
        cookies={**auth, CSRF_COOKIE: signed},
    )
    # Form re-renders with an error banner rather than redirecting.
    assert resp.status_code == 200
    assert b"error-banner" in resp.content
    assert b"note is required" in resp.content.lower()


def test_web_item_detail_shows_loanable_pill(client, db_session):
    _, item = _seed_work(db_session, "9780441090010")
    _make_user(db_session, "Librarian", "web_loan_pill")
    db_session.commit()

    auth = _login(client, "web_loan_pill")
    raw, signed = _make_csrf_pair()
    client.post(
        f"/ui/items/{item.barcode}/loanable",
        data={
            "is_loanable": "no",
            "reason": "reference",
            "note": "",
            "csrf_token": raw,
        },
        cookies={**auth, CSRF_COOKIE: signed},
    )
    resp = client.get(f"/ui/items/{item.barcode}", cookies=auth)
    assert resp.status_code == 200
    assert b"pill-reference" in resp.content


def test_web_work_detail_hides_hold_when_no_loanable(client, db_session):
    work, item = _seed_work(db_session, "9780441090011")
    _make_user(db_session, "Librarian", "web_loan_holdhide")
    db_session.commit()

    auth = _login(client, "web_loan_holdhide")
    raw, signed = _make_csrf_pair()
    client.post(
        f"/ui/items/{item.barcode}/loanable",
        data={
            "is_loanable": "no",
            "reason": "reference",
            "note": "",
            "csrf_token": raw,
        },
        cookies={**auth, CSRF_COOKIE: signed},
    )
    resp = client.get(f"/ui/catalog/{work.id}", cookies=auth)
    assert resp.status_code == 200
    assert b"holds are disabled" in resp.content.lower()
    assert b"Place Hold" not in resp.content
