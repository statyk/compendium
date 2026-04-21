"""Integration tests for REST PATCH/PUT endpoints: items, works, creators, branches."""

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
from tests.helpers import setup_sqlite_fts

_SETTINGS = Settings(
    database_url="sqlite:///:memory:",
    jwt_secret_key="insecure-default-change-in-production",
)

_OPEN_LIB_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}


@pytest.fixture(scope="module")
def patch_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    setup_sqlite_fts(engine)
    return engine


@pytest.fixture
def patch_session(patch_engine) -> Session:
    factory = sessionmaker(bind=patch_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def patch_client(patch_engine, patch_session):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=patch_engine, autoflush=False, expire_on_commit=False)
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


def _make_user(s: Session, role_name: str, username: str) -> tuple[AppUser, str]:
    role = SqlRoleRepository(s).get_by_name(role_name)
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


def _catalog(s: Session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(s),
        item_repo=SqlItemRepository(s),
        creator_repo=SqlCreatorRepository(s),
        branch_repo=SqlBranchRepository(s),
        media_type_repo=SqlMediaTypeRepository(s),
    )


def _seed_work(s: Session, isbn: str):
    global _counter
    _counter += 1
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        work, item = _catalog(s).add_from_isbn(isbn)
    s.commit()
    return work, item


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── PATCH /items/{barcode} ────────────────────────────────────────────────────

class TestItemPatch:
    def test_happy_path_updates_location(self, patch_client, patch_session):
        _, item = _seed_work(patch_session, "9780441011111")
        _, token = _make_user(patch_session, "Librarian", "item_patch_lib")
        patch_session.commit()

        resp = patch_client.patch(
            f"/items/{item.barcode}",
            json={"location": "Shelf A-3"},
            headers=_bearer(token),
        )

        assert resp.status_code == 200
        assert resp.json()["location"] == "Shelf A-3"

    def test_omitted_field_leaves_prior_value(self, patch_client, patch_session):
        _, item = _seed_work(patch_session, "9780441011112")
        item.location = "Orig"
        item.call_number = "PS.1"
        patch_session.commit()
        _, token = _make_user(patch_session, "Librarian", "item_patch_omit")
        patch_session.commit()

        resp = patch_client.patch(
            f"/items/{item.barcode}",
            json={"location": "New"},
            headers=_bearer(token),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["location"] == "New"
        # call_number isn't in ItemDetail but verify via DB:
        patch_session.refresh(item)
        assert item.call_number == "PS.1"

    def test_null_clears_field(self, patch_client, patch_session):
        _, item = _seed_work(patch_session, "9780441011113")
        item.location = "Cart"
        patch_session.commit()
        _, token = _make_user(patch_session, "Librarian", "item_patch_null")
        patch_session.commit()

        resp = patch_client.patch(
            f"/items/{item.barcode}",
            json={"location": None},
            headers=_bearer(token),
        )

        assert resp.status_code == 200
        assert resp.json()["location"] is None

    def test_unknown_barcode_returns_404(self, patch_client, patch_session):
        _, token = _make_user(patch_session, "Librarian", "item_patch_404")
        patch_session.commit()

        resp = patch_client.patch(
            "/items/NO_SUCH",
            json={"location": "X"},
            headers=_bearer(token),
        )

        assert resp.status_code == 404

    def test_forbidden_without_item_edit(self, patch_client, patch_session):
        _, item = _seed_work(patch_session, "9780441011114")
        _, token = _make_user(patch_session, "ReadOnly", "item_patch_forbid")
        patch_session.commit()

        resp = patch_client.patch(
            f"/items/{item.barcode}",
            json={"location": "Nope"},
            headers=_bearer(token),
        )

        assert resp.status_code == 403

    def test_requires_auth(self, patch_client, patch_session):
        _, item = _seed_work(patch_session, "9780441011115")
        patch_session.commit()

        resp = patch_client.patch(f"/items/{item.barcode}", json={"location": "X"})

        assert resp.status_code == 401


# ── PATCH /works/{work_id} ────────────────────────────────────────────────────

class TestWorkPatch:
    def test_happy_path_updates_title(self, patch_client, patch_session):
        work, _ = _seed_work(patch_session, "9780441022221")
        _, token = _make_user(patch_session, "Librarian", "work_patch_lib")
        patch_session.commit()

        resp = patch_client.patch(
            f"/works/{work.id}",
            json={"title": "Dune (Annotated)"},
            headers=_bearer(token),
        )

        assert resp.status_code == 200
        assert resp.json()["title"] == "Dune (Annotated)"

    def test_empty_title_returns_422(self, patch_client, patch_session):
        work, _ = _seed_work(patch_session, "9780441022222")
        _, token = _make_user(patch_session, "Librarian", "work_patch_bad_title")
        patch_session.commit()

        resp = patch_client.patch(
            f"/works/{work.id}",
            json={"title": "   "},
            headers=_bearer(token),
        )

        assert resp.status_code == 422
        assert "title" in resp.json()["detail"].lower()

    def test_unknown_work_returns_404(self, patch_client, patch_session):
        _, token = _make_user(patch_session, "Librarian", "work_patch_404")
        patch_session.commit()

        resp = patch_client.patch(
            "/works/999999",
            json={"title": "X"},
            headers=_bearer(token),
        )

        assert resp.status_code == 404

    def test_forbidden_without_work_edit(self, patch_client, patch_session):
        work, _ = _seed_work(patch_session, "9780441022223")
        _, token = _make_user(patch_session, "ReadOnly", "work_patch_forbid")
        patch_session.commit()

        resp = patch_client.patch(
            f"/works/{work.id}",
            json={"title": "Nope"},
            headers=_bearer(token),
        )

        assert resp.status_code == 403


# ── PUT /works/{work_id}/creators ─────────────────────────────────────────────

class TestWorkCreatorsReplace:
    def test_happy_path_replaces_creators(self, patch_client, patch_session):
        work, _ = _seed_work(patch_session, "9780441033331")
        _, token = _make_user(patch_session, "Librarian", "creators_put_lib")
        patch_session.commit()

        resp = patch_client.put(
            f"/works/{work.id}/creators",
            json={"creators": [
                {"name": "Frank Herbert", "role": "author"},
                {"name": "Brian Herbert", "role": "editor"},
            ]},
            headers=_bearer(token),
        )

        assert resp.status_code == 200
        patch_session.refresh(work)
        names = {wc.creator.display_name for wc in work.creators}
        assert names == {"Frank Herbert", "Brian Herbert"}

    def test_empty_list_clears_creators(self, patch_client, patch_session):
        work, _ = _seed_work(patch_session, "9780441033332")
        _, token = _make_user(patch_session, "Librarian", "creators_put_empty")
        patch_session.commit()

        resp = patch_client.put(
            f"/works/{work.id}/creators",
            json={"creators": []},
            headers=_bearer(token),
        )

        assert resp.status_code == 200
        patch_session.refresh(work)
        assert list(work.creators) == []

    def test_bad_role_returns_422(self, patch_client, patch_session):
        work, _ = _seed_work(patch_session, "9780441033333")
        _, token = _make_user(patch_session, "Librarian", "creators_put_bad_role")
        patch_session.commit()

        resp = patch_client.put(
            f"/works/{work.id}/creators",
            json={"creators": [{"name": "X", "role": "jester"}]},
            headers=_bearer(token),
        )

        assert resp.status_code == 422
        assert "jester" in resp.json()["detail"].lower()

    def test_unknown_work_returns_404(self, patch_client, patch_session):
        _, token = _make_user(patch_session, "Librarian", "creators_put_404")
        patch_session.commit()

        resp = patch_client.put(
            "/works/999999/creators",
            json={"creators": []},
            headers=_bearer(token),
        )

        assert resp.status_code == 404

    def test_forbidden_without_work_edit(self, patch_client, patch_session):
        work, _ = _seed_work(patch_session, "9780441033334")
        _, token = _make_user(patch_session, "ReadOnly", "creators_put_forbid")
        patch_session.commit()

        resp = patch_client.put(
            f"/works/{work.id}/creators",
            json={"creators": []},
            headers=_bearer(token),
        )

        assert resp.status_code == 403


# ── PATCH /creators/{creator_id} ──────────────────────────────────────────────

class TestCreatorPatch:
    def test_happy_path_renames(self, patch_client, patch_session):
        work, _ = _seed_work(patch_session, "9780441044441")
        patch_session.refresh(work)
        creator_id = work.creators[0].creator.id
        _, token = _make_user(patch_session, "Librarian", "creator_patch_lib")
        patch_session.commit()

        resp = patch_client.patch(
            f"/creators/{creator_id}",
            json={"display_name": "F. Herbert"},
            headers=_bearer(token),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["display_name"] == "F. Herbert"
        assert body["sort_name"] == "Herbert, F."

    def test_empty_name_returns_422(self, patch_client, patch_session):
        work, _ = _seed_work(patch_session, "9780441044442")
        patch_session.refresh(work)
        creator_id = work.creators[0].creator.id
        _, token = _make_user(patch_session, "Librarian", "creator_patch_empty")
        patch_session.commit()

        resp = patch_client.patch(
            f"/creators/{creator_id}",
            json={"display_name": "  "},
            headers=_bearer(token),
        )

        assert resp.status_code == 422

    def test_unknown_creator_returns_404(self, patch_client, patch_session):
        _, token = _make_user(patch_session, "Librarian", "creator_patch_404")
        patch_session.commit()

        resp = patch_client.patch(
            "/creators/999999",
            json={"display_name": "X"},
            headers=_bearer(token),
        )

        assert resp.status_code == 404

    def test_forbidden_without_work_edit(self, patch_client, patch_session):
        work, _ = _seed_work(patch_session, "9780441044443")
        patch_session.refresh(work)
        creator_id = work.creators[0].creator.id
        _, token = _make_user(patch_session, "ReadOnly", "creator_patch_forbid")
        patch_session.commit()

        resp = patch_client.patch(
            f"/creators/{creator_id}",
            json={"display_name": "Nope"},
            headers=_bearer(token),
        )

        assert resp.status_code == 403


# ── PATCH /branches/{branch_id} ───────────────────────────────────────────────

class TestBranchPatch:
    def test_happy_path_sets_scheme(self, patch_client, patch_session):
        branch = SqlBranchRepository(patch_session).get_default()
        _, token = _make_user(patch_session, "Librarian", "branch_patch_lib")
        patch_session.commit()

        resp = patch_client.patch(
            f"/branches/{branch.id}",
            json={"default_classification_scheme": "ddc"},
            headers=_bearer(token),
        )

        assert resp.status_code == 200
        assert resp.json()["default_classification_scheme"] == "ddc"

    def test_invalid_scheme_returns_422(self, patch_client, patch_session):
        branch = SqlBranchRepository(patch_session).get_default()
        _, token = _make_user(patch_session, "Librarian", "branch_patch_bad")
        patch_session.commit()

        resp = patch_client.patch(
            f"/branches/{branch.id}",
            json={"default_classification_scheme": "udc"},
            headers=_bearer(token),
        )

        assert resp.status_code == 422

    def test_unknown_branch_returns_404(self, patch_client, patch_session):
        _, token = _make_user(patch_session, "Librarian", "branch_patch_404")
        patch_session.commit()

        resp = patch_client.patch(
            "/branches/999999",
            json={"default_classification_scheme": "lcc"},
            headers=_bearer(token),
        )

        assert resp.status_code == 404

    def test_forbidden_without_branch_edit(self, patch_client, patch_session):
        branch = SqlBranchRepository(patch_session).get_default()
        _, token = _make_user(patch_session, "ReadOnly", "branch_patch_forbid")
        patch_session.commit()

        resp = patch_client.patch(
            f"/branches/{branch.id}",
            json={"default_classification_scheme": "ddc"},
            headers=_bearer(token),
        )

        assert resp.status_code == 403
