"""Integration tests for the work-deletion (trash) web UI routes."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base, Item, Loan, MediaType, Patron, Work
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)

_counter = iter(range(1, 1_000_000))


def _csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, _derive_csrf_secret(_SECRET))}"


@pytest.fixture(scope="module")
def trashw_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def trashw_session(trashw_engine):
    factory = sessionmaker(bind=trashw_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def trashw_client(trashw_session):
    from unittest.mock import patch
    app = create_app()
    app.dependency_overrides[get_session] = lambda: trashw_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


def _login(client, session, username, role_name) -> dict:
    role = SqlRoleRepository(session).get_by_name(role_name)
    user = AppUser(
        username=username, password_hash=hash_password("secret"), role_id=role.id
    )
    SqlUserRepository(session).add(user)
    session.commit()
    raw, signed = _csrf_pair()
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": "secret", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code in (200, 302, 303), f"Login failed: {resp.status_code}"
    return {"cookies": resp.cookies}


def _get_csrf(client, url: str, cookies: dict) -> str:
    resp = client.get(url, cookies=cookies)
    assert resp.status_code == 200
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.text)
    assert m, f"CSRF token not found on {url}"
    return m.group(1)


def _seed_work(session, title="Web Deletable") -> Work:
    mt = session.query(MediaType).filter_by(code="book").one()
    w = Work(title=title, media_type_id=mt.id)
    session.add(w)
    session.flush()
    branch = SqlBranchRepository(session).get_default()
    n = next(_counter)
    it = Item(
        work_id=w.id,
        branch_id=branch.id,
        barcode=f"TRW{n:08d}",
        accession_number=f"TRWACC{n:06d}",
    )
    session.add(it)
    session.flush()
    work_id = w.id
    session.commit()
    return session.get(Work, work_id)


def _add_active_loan(session, work: Work) -> None:
    branch = SqlBranchRepository(session).get_default()
    n = next(_counter)
    p = Patron(
        library_card_number=f"TRWCARD{n:08d}",
        full_name=f"Trash Patron {n}",
        is_active=True,
    )
    session.add(p)
    session.flush()
    now = datetime.now(timezone.utc)
    ln = Loan(
        item_id=work.items[0].id,
        patron_id=p.id,
        branch_id=branch.id,
        checked_out_at=now - timedelta(days=1),
        due_at=now + timedelta(days=13),
        returned_at=None,
    )
    session.add(ln)
    session.commit()


def test_delete_confirm_page_renders(trashw_client, trashw_session):
    info = _login(trashw_client, trashw_session, "trw_confirm", "Librarian")
    work = _seed_work(trashw_session, "Confirmable Title")
    resp = trashw_client.get(
        f"/ui/catalog/{work.id}/delete-confirm", cookies=info["cookies"]
    )
    assert resp.status_code == 200
    assert "Delete work" in resp.text
    assert "cannot be undone until purged" in resp.text


def test_delete_then_trash_page_then_restore(trashw_client, trashw_session):
    info = _login(trashw_client, trashw_session, "trw_flow", "Librarian")
    work = _seed_work(trashw_session, "Web Deletable")
    csrf = _get_csrf(
        trashw_client, f"/ui/catalog/{work.id}/delete-confirm", info["cookies"]
    )

    resp = trashw_client.post(
        f"/ui/catalog/{work.id}/delete",
        data={"csrf_token": csrf},
        cookies=info["cookies"],
    )
    assert resp.status_code == 303
    assert "/ui/trash" in resp.headers["location"]

    resp = trashw_client.get("/ui/trash", cookies=info["cookies"])
    assert resp.status_code == 200
    assert "Web Deletable" in resp.text
    m = re.search(r"/ui/trash/(\d+)/restore", resp.text)
    assert m, "restore link not found on trash page"
    trash_id = int(m.group(1))

    csrf = _get_csrf(trashw_client, "/ui/trash", info["cookies"])
    resp = trashw_client.post(
        f"/ui/trash/{trash_id}/restore",
        data={"csrf_token": csrf},
        cookies=info["cookies"],
    )
    assert resp.status_code == 303
    assert "/ui/catalog/" in resp.headers["location"]


def test_trash_requires_permission(trashw_client, trashw_session):
    info = _login(trashw_client, trashw_session, "trw_patron", "Patron")
    resp = trashw_client.get("/ui/trash", cookies=info["cookies"])
    assert resp.status_code in (302, 303, 403)


def test_delete_blocked_shows_error_redirect(trashw_client, trashw_session):
    info = _login(trashw_client, trashw_session, "trw_blocked", "Librarian")
    work = _seed_work(trashw_session, "Loaned Out")
    _add_active_loan(trashw_session, work)
    csrf = _get_csrf(
        trashw_client, f"/ui/catalog/{work.id}/delete-confirm", info["cookies"]
    )

    resp = trashw_client.post(
        f"/ui/catalog/{work.id}/delete",
        data={"csrf_token": csrf},
        cookies=info["cookies"],
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


def test_purge_confirm_and_purge(trashw_client, trashw_session):
    info = _login(trashw_client, trashw_session, "trw_purge", "Librarian")
    work = _seed_work(trashw_session, "Purge Me")
    csrf = _get_csrf(
        trashw_client, f"/ui/catalog/{work.id}/delete-confirm", info["cookies"]
    )
    resp = trashw_client.post(
        f"/ui/catalog/{work.id}/delete",
        data={"csrf_token": csrf},
        cookies=info["cookies"],
    )
    assert resp.status_code == 303

    resp = trashw_client.get("/ui/trash", cookies=info["cookies"])
    m = re.search(r"/ui/trash/(\d+)/purge-confirm", resp.text)
    assert m, "purge-confirm link not found"
    trash_id = int(m.group(1))

    resp = trashw_client.get(
        f"/ui/trash/{trash_id}/purge-confirm", cookies=info["cookies"]
    )
    assert resp.status_code == 200
    assert "Delete forever" in resp.text

    csrf = _get_csrf(
        trashw_client, f"/ui/trash/{trash_id}/purge-confirm", info["cookies"]
    )
    resp = trashw_client.post(
        f"/ui/trash/{trash_id}/purge",
        data={"csrf_token": csrf},
        cookies=info["cookies"],
    )
    assert resp.status_code == 303
    assert "/ui/trash" in resp.headers["location"]
