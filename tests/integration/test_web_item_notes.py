"""Integration: Web UI routes for Item Notes."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base, Branch, Item, ItemNote, Work
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from tests.helpers import setup_sqlite_fts, TEST_SECRET

_CSRF_KEY = _derive_csrf_secret(TEST_SECRET)


@pytest.fixture(scope="module")
def web_engine():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(e)
    setup_sqlite_fts(e)
    return e


@pytest.fixture
def db(web_engine):
    factory = sessionmaker(bind=web_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def client(web_engine, db):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=web_engine, autoflush=False, expire_on_commit=False)
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
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _make_csrf_pair() -> tuple[str, str]:
    """Return (raw_token, signed_cookie_value) for test requests."""
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _CSRF_KEY)}"
    return raw, signed


def _make_librarian(db: Session) -> tuple[str, str]:
    """Create a librarian user; return (username, password)."""
    role = SqlRoleRepository(db).get_by_name("Librarian")
    username = f"web_lib_notes_{id(db)}"
    u = AppUser(username=username, password_hash=hash_password("Str0ng!Pass"), role_id=role.id)
    SqlUserRepository(db).add(u)
    db.commit()
    return username, "Str0ng!Pass"


def _login(client: TestClient, username: str, password: str) -> None:
    """Log in via /ui/login; cookies are persisted on the client instance."""
    raw, signed = _make_csrf_pair()
    r = client.post(
        "/ui/login",
        data={"username": username, "password": password, "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert r.status_code == 303, f"Login failed: {r.status_code}"


def _csrf_from_page(client: TestClient, path: str) -> str:
    """Fetch a page and extract the CSRF token."""
    r = client.get(path)
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    return m.group(1) if m else ""


def _make_item(db: Session, barcode: str = "TEST-BC-001") -> Item:
    """Create a minimal Work + Item for testing."""
    from compendium.domain.models import MediaType
    branch = db.query(Branch).first()
    media_type = db.query(MediaType).filter_by(code="book").first()
    work = Work(
        title="Test Book for Notes",
        media_type_id=media_type.id,
    )
    db.add(work)
    db.flush()
    item = Item(
        barcode=barcode,
        accession_number=f"ACC-{barcode}",
        work_id=work.id,
        branch_id=branch.id,
    )
    db.add(item)
    db.commit()
    return item


# ── Tests ────────────────────────────────────────────────────────────────────


def test_item_detail_shows_notes_section(client, db):
    """Item detail page renders the 'Notes & history' heading."""
    item = _make_item(db, "NOTE-BC-001")
    username, pw = _make_librarian(db)
    _login(client, username, pw)

    r = client.get(f"/ui/items/{item.barcode}")
    assert r.status_code == 200
    assert "Notes &amp; history" in r.text or "Notes & history" in r.text


def test_post_note_redirects_and_note_appears(client, db):
    """POSTing a note redirects back; the note then appears on the detail page."""
    item = _make_item(db, "NOTE-BC-002")
    username, pw = _make_librarian(db)
    _login(client, username, pw)

    csrf_token = _csrf_from_page(client, f"/ui/items/{item.barcode}")
    r = client.post(
        f"/ui/items/{item.barcode}/notes/add",
        data={
            "kind": "condition",
            "note": "Minor spine wear noted.",
            "event_date": "",
            "csrf_token": csrf_token,
        },
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert f"/ui/items/{item.barcode}" in location

    # Follow redirect and check that the note is on the page
    r2 = client.get(f"/ui/items/{item.barcode}")
    assert r2.status_code == 200
    assert "Minor spine wear noted." in r2.text


def test_delete_note_removes_it(client, db):
    """Deleting a manual note removes it from the detail page."""
    item = _make_item(db, "NOTE-BC-003")
    # Add a note directly via repository
    note = ItemNote(
        item_id=item.id,
        kind="general",
        note="Note to be deleted.",
        is_system=False,
    )
    SqlItemNoteRepository(db).add(note)
    db.commit()

    username, pw = _make_librarian(db)
    _login(client, username, pw)

    # Confirm the note is on the page before deletion
    r = client.get(f"/ui/items/{item.barcode}")
    assert r.status_code == 200
    assert "Note to be deleted." in r.text

    csrf_token = _csrf_from_page(client, f"/ui/items/{item.barcode}")
    r2 = client.post(
        f"/ui/items/{item.barcode}/notes/{note.id}/delete",
        data={"csrf_token": csrf_token},
    )
    assert r2.status_code == 303

    # Note should be gone from the detail page
    r3 = client.get(f"/ui/items/{item.barcode}")
    assert r3.status_code == 200
    assert "Note to be deleted." not in r3.text


def test_system_note_has_no_delete_button(client, db):
    """System-generated notes do not render a Delete button."""
    item = _make_item(db, "NOTE-BC-004")
    sys_note = ItemNote(
        item_id=item.id,
        kind="status",
        note="Status changed to available.",
        is_system=True,
    )
    SqlItemNoteRepository(db).add(sys_note)
    db.commit()

    username, pw = _make_librarian(db)
    _login(client, username, pw)

    r = client.get(f"/ui/items/{item.barcode}")
    assert r.status_code == 200
    assert "Status changed to available." in r.text
    # Delete form action for this specific note must not appear
    assert f"/notes/{sys_note.id}/delete" not in r.text


def test_post_blank_note_redirects_with_error(client, db):
    """Submitting a blank note redirects back to the detail page with an error param."""
    item = _make_item(db, "NOTE-BC-005")
    username, pw = _make_librarian(db)
    _login(client, username, pw)

    csrf_token = _csrf_from_page(client, f"/ui/items/{item.barcode}")
    r = client.post(
        f"/ui/items/{item.barcode}/notes/add",
        data={
            "kind": "general",
            "note": "   ",  # blank / whitespace only
            "event_date": "",
            "csrf_token": csrf_token,
        },
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert f"/ui/items/{item.barcode}" in location
    assert "error=" in location
