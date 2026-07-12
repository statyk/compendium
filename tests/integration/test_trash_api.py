"""API tests for DELETE /works/{id} and the /trash router (recoverable work
deletion). Client/auth fixtures adapted from tests/integration/test_api_authz.py."""

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
from compendium.domain.models import (
    AppUser,
    Base,
    Branch,
    Item,
    Loan,
    MediaType,
    Patron,
    Work,
)
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService, hash_password
from tests.helpers import setup_sqlite_fts

_SETTINGS = Settings(
    database_url="sqlite:///:memory:",
    jwt_secret_key="insecure-default-change-in-production",
)


@pytest.fixture(scope="module")
def trash_api_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    setup_sqlite_fts(engine)
    return engine


@pytest.fixture
def trash_api_session(trash_api_engine) -> Session:
    factory = sessionmaker(bind=trash_api_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def trash_api_client(trash_api_engine, trash_api_session):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=trash_api_engine, autoflush=False, expire_on_commit=False)
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
    return TestClient(app)


_counter = 0


def _make_patron_user(s: Session, username: str) -> tuple[AppUser, str]:
    global _counter
    _counter += 1
    role = SqlRoleRepository(s).get_by_name("Patron")
    user = AppUser(username=username, password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(s).add(user)
    s.flush()
    user.role = role
    token = AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(user)
    return user, token


def _make_librarian(s: Session, username: str) -> tuple[AppUser, str]:
    role = SqlRoleRepository(s).get_by_name("Administrator")
    user = AppUser(username=username, password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(s).add(user)
    s.flush()
    user.role = role
    token = AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_SETTINGS,
    ).issue_token(user)
    return user, token


def _mk_work(session: Session, *, title: str, isbn: str, with_active_loan: bool = False) -> Work:
    """A single-item work, mirroring tests/integration/test_trash.py::_mk_work
    but trimmed to what these API tests need."""
    branch = session.query(Branch).first()
    media = session.query(MediaType).filter_by(code="book").first()
    work = Work(title=title, media_type_id=media.id, isbn=isbn, search_text=title)
    session.add(work)
    session.flush()
    item = Item(
        work_id=work.id, branch_id=branch.id,
        barcode=f"BC-{isbn}", accession_number=f"ACC-{isbn}",
    )
    session.add(item)
    session.flush()
    if with_active_loan:
        patron = Patron(library_card_number=f"CARD-{isbn}", full_name="Pat Ron")
        session.add(patron)
        session.flush()
        session.add(Loan(
            item_id=item.id, patron_id=patron.id, branch_id=branch.id,
            due_at=datetime.now(timezone.utc) + timedelta(days=14),
        ))
        session.flush()
    return work


def test_delete_work_requires_permission(trash_api_client, trash_api_session):
    work = _mk_work(trash_api_session, title="Guarded", isbn="9781000000001")
    trash_api_session.commit()
    _, token = _make_patron_user(trash_api_session, "trash_patron_denied")

    resp = trash_api_client.delete(
        f"/works/{work.id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


def test_delete_restore_flow(trash_api_client, trash_api_session):
    work = _mk_work(trash_api_session, title="API Deletable", isbn="9781000000002")
    trash_api_session.commit()
    work_id = work.id
    _, token = _make_librarian(trash_api_session, "trash_lib_flow")
    headers = {"Authorization": f"Bearer {token}"}

    resp = trash_api_client.delete(f"/works/{work_id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["item_count"] == 1
    assert body["original_work_id"] == work_id

    listed = trash_api_client.get("/trash", headers=headers)
    assert listed.status_code == 200
    assert any(r["trash_id"] == body["trash_id"] for r in listed.json())

    restored = trash_api_client.post(f"/trash/{body['trash_id']}/restore", headers=headers)
    assert restored.status_code == 200
    assert restored.json()["title"] == "API Deletable"


def test_delete_work_blocked_returns_409(trash_api_client, trash_api_session):
    work = _mk_work(
        trash_api_session, title="Blocked", isbn="9781000000003", with_active_loan=True
    )
    trash_api_session.commit()
    _, token = _make_librarian(trash_api_session, "trash_lib_blocked")

    resp = trash_api_client.delete(
        f"/works/{work.id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 409


def test_delete_work_not_found_returns_404(trash_api_client, trash_api_session):
    _, token = _make_librarian(trash_api_session, "trash_lib_404")

    resp = trash_api_client.delete(
        "/works/999999", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404


def test_purge_trash_entry(trash_api_client, trash_api_session):
    work = _mk_work(trash_api_session, title="Purgeable", isbn="9781000000004")
    trash_api_session.commit()
    _, token = _make_librarian(trash_api_session, "trash_lib_purge")
    headers = {"Authorization": f"Bearer {token}"}

    deleted = trash_api_client.delete(f"/works/{work.id}", headers=headers)
    trash_id = deleted.json()["trash_id"]

    resp = trash_api_client.delete(f"/trash/{trash_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["purged"] == 1

    listed = trash_api_client.get("/trash", headers=headers)
    assert all(r["trash_id"] != trash_id for r in listed.json())


def test_purge_missing_trash_entry_returns_404(trash_api_client, trash_api_session):
    _, token = _make_librarian(trash_api_session, "trash_lib_purge_404")

    resp = trash_api_client.delete(
        "/trash/999999", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404


def test_restore_missing_trash_entry_returns_404(trash_api_client, trash_api_session):
    _, token = _make_librarian(trash_api_session, "trash_lib_restore_404")

    resp = trash_api_client.post(
        "/trash/999999/restore", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404


def test_trash_endpoints_require_permission(trash_api_client, trash_api_session):
    # Each 403 below raises through the still-open get_session dependency,
    # which rolls back the shared StaticPool connection (see _override); commit
    # first so the patron user survives across these sequential requests.
    _, token = _make_patron_user(trash_api_session, "trash_patron_denied2")
    trash_api_session.commit()
    headers = {"Authorization": f"Bearer {token}"}

    assert trash_api_client.get("/trash", headers=headers).status_code == 403
    assert trash_api_client.post("/trash/1/restore", headers=headers).status_code == 403
    assert trash_api_client.delete("/trash/1", headers=headers).status_code == 403
