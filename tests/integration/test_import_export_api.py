"""API tests for /import/* and /export/* endpoints."""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pymarc import Field, MARCWriter, Record, Subfield
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base
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


_counter = 0


def _make_user(s: Session, role_name: str, username: str) -> tuple[AppUser, str]:
    role = SqlRoleRepository(s).get_by_name(role_name)
    user = AppUser(
        username=username,
        password_hash=hash_password("password"),
        role_id=role.id,
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


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _minimal_csv_bytes(isbn="9780441013593") -> bytes:
    return (
        "media_type,title,authors,isbn\n"
        f"book,Dune,Frank Herbert,{isbn}\n"
    ).encode("utf-8")


def _minimal_lt_tsv_bytes(isbn="9780441013593") -> bytes:
    return (
        "Title\tPrimary Author\tPublication\tDate\tMedia\tLanguages\tISBN\tCopies\n"
        f"Dune\tHerbert, Frank\tAce (1965), Paperback\t1965\tPaperback\tEnglish\t[{isbn}]\t1\n"
    ).encode("utf-8")


def _marc_bytes(isbn="9780441013601") -> bytes:
    r = Record()
    leader = list(r.leader)
    while len(leader) < 24:
        leader.append(" ")
    leader[6] = "a"
    r.leader = "".join(leader)
    r.add_field(Field(tag="020", indicators=[" ", " "], subfields=[Subfield("a", isbn)]))
    r.add_field(
        Field(tag="100", indicators=["1", " "], subfields=[Subfield("a", "Author X")])
    )
    r.add_field(
        Field(tag="245", indicators=["1", "0"], subfields=[Subfield("a", "A Book /")])
    )
    buf = io.BytesIO()
    w = MARCWriter(buf)
    w.write(r)
    w.close(close_fh=False)
    return buf.getvalue()


def test_api_import_csv_happy_path(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_imp_csv")
    db_session.commit()

    resp = client.post(
        "/import/csv",
        files={"file": ("books.csv", _minimal_csv_bytes(), "text/csv")},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "csv"
    assert body["created_works"] == 1
    assert body["errors"] == []
    assert body["dry_run"] is False


def test_api_import_csv_dry_run(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_imp_dry")
    db_session.commit()

    resp = client.post(
        "/import/csv?dry_run=true",
        files={"file": ("books.csv", _minimal_csv_bytes("9780441013594"), "text/csv")},
        headers=_bearer(token),
    )
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True


def test_api_import_csv_requires_catalog_import_permission(client, db_session):
    _, token = _make_user(db_session, "ReadOnly", "api_imp_forbid")
    db_session.commit()

    resp = client.post(
        "/import/csv",
        files={"file": ("books.csv", _minimal_csv_bytes("9780441013595"), "text/csv")},
        headers=_bearer(token),
    )
    assert resp.status_code == 403


def test_api_import_csv_requires_auth(client):
    resp = client.post(
        "/import/csv",
        files={"file": ("books.csv", _minimal_csv_bytes("9780441013596"), "text/csv")},
    )
    assert resp.status_code == 401


def test_api_import_csv_invalid_mode_returns_422(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_imp_mode")
    db_session.commit()

    resp = client.post(
        "/import/csv?mode=bogus",
        files={"file": ("books.csv", _minimal_csv_bytes("9780441013597"), "text/csv")},
        headers=_bearer(token),
    )
    assert resp.status_code == 422
    assert "mode" in resp.json()["detail"].lower()


def test_api_import_librarything_happy_path(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_imp_lt")
    db_session.commit()

    resp = client.post(
        "/import/librarything",
        files={
            "file": (
                "books.tsv",
                _minimal_lt_tsv_bytes("9780441013700"),
                "text/tab-separated-values",
            )
        },
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "librarything"
    assert body["errors"] == []
    assert body["warnings"] == []
    # The module-scoped engine accumulates rows; assert at least one work
    # came from this call (created or added_copy of an earlier ISBN).
    assert body["created_works"] + body["added_copies"] >= 1


def test_api_import_librarything_strict_encoding_rejects_stray_byte(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_imp_lt_strict")
    db_session.commit()

    raw = _minimal_lt_tsv_bytes("9780441013594").replace(b"Dune", b"D\xe8ne")
    resp = client.post(
        "/import/librarything?strict_encoding=true",
        files={"file": ("messy.tsv", raw, "text/tab-separated-values")},
        headers=_bearer(token),
    )
    assert resp.status_code == 422
    assert "utf-8" in resp.json()["detail"].lower()


def test_api_import_librarything_lenient_imports_with_warning(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_imp_lt_lenient")
    db_session.commit()

    raw = _minimal_lt_tsv_bytes("9780441013595").replace(b"Dune", b"D\xe8ne")
    resp = client.post(
        "/import/librarything",
        files={"file": ("messy.tsv", raw, "text/tab-separated-values")},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created_works"] == 1
    assert any("byte replacement" in w.lower() for w in body["warnings"])


def _minimal_gr_csv_bytes(isbn13="9780140067477", isbn10="0140067477") -> bytes:
    # GoodReads wraps ISBNs in Excel-style ="..." to prevent numeric coercion.
    # In the raw CSV the field is CSV-quoted so the inner " chars are doubled:
    # "=""0140067477""" (outer "" are CSV quoting; inner "" are escaped quotes).
    buf = io.StringIO()
    w = __import__("csv").writer(buf)
    w.writerow([
        "Book Id", "Title", "Author", "Author l-f", "Additional Authors",
        "ISBN", "ISBN13", "My Rating", "Publisher", "Binding",
        "Number of Pages", "Year Published", "Original Publication Year",
        "Date Read", "Date Added", "Bookshelves", "Bookshelves with positions",
        "Exclusive Shelf", "My Review", "Spoiler", "Private Notes",
        "Read Count", "Owned Copies",
    ])
    w.writerow([
        "242337159", "The Tao of Pooh", "Benjamin Hoff", "Hoff, Benjamin", "",
        f'="{isbn10}"', f'="{isbn13}"', "4", "Penguin Books", "Paperback",
        "158", "1983", "1982", "", "2026/05/08", "", "", "read", "", "", "", "1", "1",
    ])
    return buf.getvalue().encode("utf-8")


def test_api_import_goodreads_happy_path(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_imp_gr")
    db_session.commit()

    resp = client.post(
        "/import/goodreads",
        files={
            "file": (
                "goodreads.csv",
                _minimal_gr_csv_bytes("9780140067477"),
                "text/csv",
            )
        },
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "goodreads"
    assert body["errors"] == []
    assert body["warnings"] == []
    assert body["created_works"] + body["added_copies"] >= 1


def test_api_import_goodreads_strict_encoding_rejects_stray_byte(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_imp_gr_strict")
    db_session.commit()

    raw = _minimal_gr_csv_bytes("9780140067490").replace(b"Pooh", b"P\xe8oh")
    resp = client.post(
        "/import/goodreads?strict_encoding=true",
        files={"file": ("messy.csv", raw, "text/csv")},
        headers=_bearer(token),
    )
    assert resp.status_code == 422
    assert "utf-8" in resp.json()["detail"].lower()


def test_api_import_goodreads_lenient_imports_with_warning(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_imp_gr_lenient")
    db_session.commit()

    raw = _minimal_gr_csv_bytes("9780140067491").replace(b"Pooh", b"P\xe8oh")
    resp = client.post(
        "/import/goodreads",
        files={"file": ("messy.csv", raw, "text/csv")},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created_works"] == 1
    assert any("byte replacement" in w.lower() for w in body["warnings"])


def test_api_import_marc_happy_path(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_imp_marc")
    db_session.commit()

    resp = client.post(
        "/import/marc",
        files={"file": ("sample.mrc", _marc_bytes("9780441013602"), "application/marc")},
        headers=_bearer(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "marc"
    assert body["created_works"] == 1


def test_api_export_csv_returns_file(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_exp_csv")
    db_session.commit()

    client.post(
        "/import/csv",
        files={"file": ("books.csv", _minimal_csv_bytes("9780441013603"), "text/csv")},
        headers=_bearer(token),
    )
    resp = client.get("/export/csv", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "Dune" in resp.text


def test_api_export_marc_returns_bytes(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_exp_marc")
    db_session.commit()

    client.post(
        "/import/marc",
        files={"file": ("sample.mrc", _marc_bytes("9780441013604"), "application/marc")},
        headers=_bearer(token),
    )
    resp = client.get("/export/marc", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/marc")
    assert len(resp.content) > 0


def test_api_export_marcxml(client, db_session):
    _, token = _make_user(db_session, "Librarian", "api_exp_xml")
    db_session.commit()

    client.post(
        "/import/marc",
        files={"file": ("sample.mrc", _marc_bytes("9780441013605"), "application/marc")},
        headers=_bearer(token),
    )
    resp = client.get("/export/marc?xml=true", headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.text.startswith("<?xml")


def test_api_export_requires_auth(client):
    resp = client.get("/export/csv")
    assert resp.status_code == 401
