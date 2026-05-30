# tests/integration/test_api_item_notes.py
"""Integration: REST API endpoints for Item Notes."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base, Branch, Item, ItemNote, MediaType, Work
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService, hash_password
from tests.helpers import setup_sqlite_fts

_TEST_SETTINGS = Settings(
    database_url="sqlite:///:memory:",
    jwt_secret_key="insecure-default-change-in-production",
)


def _issue_token(s: Session, user: AppUser) -> str:
    return AuthService(
        user_repo=SqlUserRepository(s),
        role_repo=SqlRoleRepository(s),
        settings=_TEST_SETTINGS,
    ).issue_token(user)


@pytest.fixture(scope="module")
def api_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    setup_sqlite_fts(engine)
    return engine


@pytest.fixture
def db(api_engine) -> Session:
    factory = sessionmaker(bind=api_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def client(api_engine, db):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=api_engine, autoflush=False, expire_on_commit=False)
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


def _make_user(s: Session, username: str, role_name: str) -> AppUser:
    role = SqlRoleRepository(s).get_by_name(role_name)
    u = AppUser(username=username, password_hash=hash_password("Str0ng!Pass"), role_id=role.id)
    u = SqlUserRepository(s).add(u)
    s.flush()
    u.role = role
    return u


def _auth_header(s: Session, username: str, role_name: str) -> dict:
    user = _make_user(s, username, role_name)
    s.commit()
    token = _issue_token(s, user)
    return {"Authorization": f"Bearer {token}"}


_BARCODE_CTR = [0]


def _make_item(db: Session) -> Item:
    """Create and flush a minimal work + item, returning the item."""
    _BARCODE_CTR[0] += 1
    n = _BARCODE_CTR[0]
    media_type = db.execute(select(MediaType).where(MediaType.code == "book")).scalar_one()
    branch = db.execute(select(Branch).where(Branch.is_default == True)).scalar_one()  # noqa: E712
    work = Work(
        title=f"Test Work for Notes {n}",
        media_type_id=media_type.id,
    )
    db.add(work)
    db.flush()
    item = Item(
        work_id=work.id,
        branch_id=branch.id,
        barcode=f"API-NOTE-{n:04d}",
        accession_number=f"API-ACC-{n:04d}",
    )
    db.add(item)
    db.commit()
    return item


class TestGetItemNotes:
    def test_librarian_gets_empty_list(self, client, db):
        """GET /items/{barcode}/notes returns empty list for a new item."""
        item = _make_item(db)
        headers = _auth_header(db, "lib_get1", "Librarian")
        r = client.get(f"/items/{item.barcode}/notes", headers=headers)
        assert r.status_code == 200
        assert r.json() == []

    def test_not_found_returns_404(self, client, db):
        """GET on non-existent barcode returns 404."""
        headers = _auth_header(db, "lib_get2", "Librarian")
        r = client.get("/items/NONEXISTENT-BARCODE/notes", headers=headers)
        assert r.status_code == 404


class TestPostItemNote:
    def test_librarian_can_post_note(self, client, db):
        """Librarian can POST a note and gets back ItemNoteResponse with 201."""
        item = _make_item(db)
        headers = _auth_header(db, "lib_post1", "Librarian")
        r = client.post(
            f"/items/{item.barcode}/notes",
            json={"note": "Spine cracked on page 42", "kind": "condition"},
            headers=headers,
        )
        assert r.status_code == 201
        data = r.json()
        assert data["note"] == "Spine cracked on page 42"
        assert data["kind"] == "condition"
        assert data["is_system"] is False
        assert data["id"] > 0
        assert "created_at" in data

    def test_patron_role_gets_403(self, client, db):
        """Patron role cannot POST a note — missing item.edit permission."""
        item = _make_item(db)
        headers = _auth_header(db, "patron_post1", "Patron")
        r = client.post(
            f"/items/{item.barcode}/notes",
            json={"note": "Some note"},
            headers=headers,
        )
        assert r.status_code == 403

    def test_blank_note_returns_422(self, client, db):
        """Blank note text is rejected with 422."""
        item = _make_item(db)
        headers = _auth_header(db, "lib_post2", "Librarian")
        r = client.post(
            f"/items/{item.barcode}/notes",
            json={"note": "   "},
            headers=headers,
        )
        assert r.status_code == 422

    def test_missing_note_field_returns_422(self, client, db):
        """Missing 'note' field in body returns 422 (Pydantic validation)."""
        item = _make_item(db)
        headers = _auth_header(db, "lib_post3", "Librarian")
        r = client.post(
            f"/items/{item.barcode}/notes",
            json={"kind": "general"},
            headers=headers,
        )
        assert r.status_code == 422

    def test_post_not_found_returns_404(self, client, db):
        """POST to non-existent barcode returns 404."""
        headers = _auth_header(db, "lib_post4", "Librarian")
        r = client.post(
            "/items/NONEXISTENT-BARCODE/notes",
            json={"note": "Some note"},
            headers=headers,
        )
        assert r.status_code == 404

    def test_default_kind_is_general(self, client, db):
        """When kind is omitted, it defaults to 'general'."""
        item = _make_item(db)
        headers = _auth_header(db, "lib_post5", "Librarian")
        r = client.post(
            f"/items/{item.barcode}/notes",
            json={"note": "A general note"},
            headers=headers,
        )
        assert r.status_code == 201
        assert r.json()["kind"] == "general"


class TestDeleteItemNote:
    def test_librarian_can_delete_manual_note(self, client, db):
        """Librarian can DELETE a manual note (204 no content)."""
        item = _make_item(db)
        # Create a manual note directly in DB
        note = ItemNote(item_id=item.id, note="To be deleted", kind="general", is_system=False)
        db.add(note)
        db.commit()

        headers = _auth_header(db, "lib_del1", "Librarian")
        r = client.delete(f"/items/{item.barcode}/notes/{note.id}", headers=headers)
        assert r.status_code == 204

    def test_cannot_delete_system_note(self, client, db):
        """Deleting a system note returns 422."""
        item = _make_item(db)
        note = ItemNote(item_id=item.id, note="Auto-generated", kind="status", is_system=True)
        db.add(note)
        db.commit()

        headers = _auth_header(db, "lib_del2", "Librarian")
        r = client.delete(f"/items/{item.barcode}/notes/{note.id}", headers=headers)
        assert r.status_code == 422

    def test_delete_nonexistent_note_returns_404(self, client, db):
        """DELETE on a note_id that doesn't exist returns 404."""
        item = _make_item(db)
        headers = _auth_header(db, "lib_del3", "Librarian")
        r = client.delete(f"/items/{item.barcode}/notes/999999", headers=headers)
        assert r.status_code == 404

    def test_delete_nonexistent_barcode_returns_404(self, client, db):
        """DELETE on a non-existent barcode returns 404."""
        headers = _auth_header(db, "lib_del4", "Librarian")
        r = client.delete("/items/NONEXISTENT-BARCODE/notes/1", headers=headers)
        assert r.status_code == 404

    def test_cannot_delete_other_items_note(self, client, db):
        """Cannot delete a note that belongs to a different item (cross-item guard)."""
        item_a = _make_item(db)
        item_b = _make_item(db)
        note = ItemNote(item_id=item_a.id, note="Belongs to A", kind="general", is_system=False)
        db.add(note)
        db.commit()

        headers = _auth_header(db, "lib_del5", "Librarian")
        r = client.delete(f"/items/{item_b.barcode}/notes/{note.id}", headers=headers)
        assert r.status_code == 404


class TestInvalidKind:
    def test_status_kind_is_rejected(self, client, db):
        """Posting kind='status' is rejected (system-only kind)."""
        item = _make_item(db)
        headers = _auth_header(db, "lib_kind1", "Librarian")
        r = client.post(
            f"/items/{item.barcode}/notes",
            json={"kind": "status", "note": "Manual status note"},
            headers=headers,
        )
        assert r.status_code == 422

    def test_invalid_kind_is_rejected(self, client, db):
        """Posting an unknown kind value returns 422."""
        item = _make_item(db)
        headers = _auth_header(db, "lib_kind2", "Librarian")
        r = client.post(
            f"/items/{item.barcode}/notes",
            json={"kind": "bogus_kind", "note": "Something"},
            headers=headers,
        )
        assert r.status_code == 422
