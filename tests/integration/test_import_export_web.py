"""Web UI tests for /ui/admin/import and /ui/admin/export."""

from __future__ import annotations

import io
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pymarc import Field, MARCWriter, Record, Subfield
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db import engine as _db_engine
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.auth import AuthService, hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    # File-based SQLite with WAL allows multiple concurrent connections.
    # Required because background import threads open their own sessions while
    # the test's main session is still active (StaticPool's single connection
    # causes OperationalError when both sessions try to use it at once).
    db_path = tmp_path_factory.mktemp("db") / "test_import_web.db"
    eng = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    with eng.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA busy_timeout=5000"))
        conn.commit()
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    yield eng
    eng.dispose()


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

    @contextmanager
    def _bg_session_scope():
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
    # Bind test engine so background import threads use the same in-memory DB.
    _db_engine.bind(engine, session_scope=_bg_session_scope)
    with patch("compendium.db.engine.get_settings", return_value=_SETTINGS):
        yield TestClient(app, follow_redirects=False)
    _db_engine.unbind()


def _make_user(s, role_name, username):
    role = SqlRoleRepository(s).get_by_name(role_name)
    u = AppUser(username=username, password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(s).add(u)
    s.flush()
    u.role = role
    return u


def _make_csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _derive_csrf_secret(_SECRET))}"
    return raw, signed


def _login(client, username, password="password") -> dict:
    raw, signed = _make_csrf_pair()
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": password, "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    return dict(resp.cookies)


def _csv_bytes(isbn="9780441013701"):
    return (
        f"media_type,title,authors,isbn\nbook,Dune,Frank Herbert,{isbn}\n".encode("utf-8")
    )


def _marc_bytes(isbn="9780441013702"):
    r = Record()
    leader = list(r.leader)
    while len(leader) < 24:
        leader.append(" ")
    leader[6] = "a"
    r.leader = "".join(leader)
    r.add_field(Field(tag="020", indicators=[" ", " "], subfields=[Subfield("a", isbn)]))
    r.add_field(Field(tag="100", indicators=["1", " "], subfields=[Subfield("a", "Author")]))
    r.add_field(Field(tag="245", indicators=["1", "0"], subfields=[Subfield("a", "Dune /")]))
    buf = io.BytesIO()
    w = MARCWriter(buf)
    w.write(r)
    w.close(close_fh=False)
    return buf.getvalue()


def _wait_for_import(client, cookies, location, timeout=5.0):
    """Poll the job status endpoint until the import finishes (done or failed)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(location + "/status", cookies=cookies)
        assert resp.status_code == 200
        # The running/pending partial includes hx-trigger; done/failed do not.
        if b"hx-trigger" not in resp.content:
            return resp
        time.sleep(0.05)
    return resp  # return last response even if still running (test will fail)


def test_web_import_form_renders(client, db_session):
    _make_user(db_session, "Librarian", "web_imp_form")
    db_session.commit()
    cookies = _login(client, "web_imp_form")
    resp = client.get("/ui/admin/import", cookies=cookies)
    assert resp.status_code == 200
    assert b"Bulk import" in resp.content
    assert b"Dry run" in resp.content


def test_web_import_csv_dry_run(client, db_session):
    _make_user(db_session, "Librarian", "web_imp_dry")
    db_session.commit()
    cookies = _login(client, "web_imp_dry")
    raw, signed = _make_csrf_pair()
    resp = client.post(
        "/ui/admin/import",
        data={
            "format": "csv",
            "mode": "append",
            "dry_run": "1",
            "default_media_type": "",
            "default_branch": "",
            "csrf_token": raw,
        },
        files={"file": ("in.csv", _csv_bytes(), "text/csv")},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "/jobs/" in location

    final = _wait_for_import(client, cookies, location)
    assert b"Dry-run complete" in final.content
    # Dry-run: nothing persisted
    assert SqlWorkRepository(db_session).get_by_isbn("9780441013701") is None


def test_web_import_csv_applies(client, db_session):
    _make_user(db_session, "Librarian", "web_imp_live")
    db_session.commit()
    cookies = _login(client, "web_imp_live")
    raw, signed = _make_csrf_pair()
    resp = client.post(
        "/ui/admin/import",
        data={
            "format": "csv",
            "mode": "append",
            "default_media_type": "",
            "default_branch": "",
            "csrf_token": raw,
        },
        files={"file": ("in.csv", _csv_bytes("9780441013703"), "text/csv")},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    location = resp.headers["location"]

    final = _wait_for_import(client, cookies, location)
    assert b"Import applied" in final.content
    db_session.expire_all()
    assert SqlWorkRepository(db_session).get_by_isbn("9780441013703") is not None


def test_web_import_forbidden_without_catalog_import(client, db_session):
    _make_user(db_session, "ReadOnly", "web_imp_forbid")
    db_session.commit()
    cookies = _login(client, "web_imp_forbid")
    resp = client.get("/ui/admin/import", cookies=cookies)
    assert resp.status_code in {302, 303, 403}


def test_web_import_csv_error_re_renders_form(client, db_session):
    """A CSV missing required columns shows an error on the import form (pre-flight check)."""
    _make_user(db_session, "Librarian", "web_imp_err")
    db_session.commit()
    cookies = _login(client, "web_imp_err")
    raw, signed = _make_csrf_pair()
    bad = b"author,isbn\nFrank Herbert,9780441013704\n"
    resp = client.post(
        "/ui/admin/import",
        data={
            "format": "csv",
            "mode": "append",
            "default_media_type": "",
            "default_branch": "",
            "csrf_token": raw,
        },
        files={"file": ("bad.csv", bad, "text/csv")},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    # Missing required column is a validation error detectable only inside the
    # import thread; the route returns 303 and the job ends in "failed" state.
    assert resp.status_code == 303
    location = resp.headers["location"]

    final = _wait_for_import(client, cookies, location)
    assert b"error-banner" in final.content


def test_web_export_form_renders(client, db_session):
    _make_user(db_session, "Librarian", "web_exp_form")
    db_session.commit()
    cookies = _login(client, "web_exp_form")
    resp = client.get("/ui/admin/export", cookies=cookies)
    assert resp.status_code == 200
    assert b"Bulk export" in resp.content


def test_web_export_csv_downloads(client, db_session):
    _make_user(db_session, "Librarian", "web_exp_csv")
    db_session.commit()
    cookies = _login(client, "web_exp_csv")
    raw, signed = _make_csrf_pair()
    # Seed one work via import; wait for completion before exporting.
    resp = client.post(
        "/ui/admin/import",
        data={
            "format": "csv",
            "mode": "append",
            "default_media_type": "",
            "default_branch": "",
            "csrf_token": raw,
        },
        files={"file": ("in.csv", _csv_bytes("9780441013705"), "text/csv")},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    _wait_for_import(client, cookies, resp.headers["location"])

    raw2, signed2 = _make_csrf_pair()
    resp = client.post(
        "/ui/admin/export",
        data={
            "format": "csv",
            "media_type": "",
            "branch": "",
            "since": "",
            "csrf_token": raw2,
        },
        cookies={**cookies, CSRF_COOKIE: signed2},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "Dune" in resp.text


def test_web_import_apply_after_dry_run(client, db_session):
    _make_user(db_session, "Librarian", "web_imp_apply")
    db_session.commit()
    cookies = _login(client, "web_imp_apply")
    raw, signed = _make_csrf_pair()
    # 1. Dry run.
    resp = client.post(
        "/ui/admin/import",
        data={
            "format": "csv", "mode": "append", "dry_run": "1",
            "default_media_type": "", "default_branch": "", "csrf_token": raw,
        },
        files={"file": ("in.csv", _csv_bytes("9780441013799"), "text/csv")},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    dry_location = resp.headers["location"]              # /ui/admin/import/jobs/<id>
    final = _wait_for_import(client, cookies, dry_location)
    assert b"Dry-run complete" in final.content
    assert b"Apply import" in final.content
    assert SqlWorkRepository(db_session).get_by_isbn("9780441013799") is None

    # 2. Click Apply.
    job_id = dry_location.rstrip("/").split("/")[-1]
    raw2, signed2 = _make_csrf_pair()
    apply_resp = client.post(
        f"/ui/admin/import/jobs/{job_id}/apply",
        data={"csrf_token": raw2},
        cookies={**cookies, CSRF_COOKIE: signed2},
    )
    assert apply_resp.status_code == 303
    apply_location = apply_resp.headers["location"]
    assert "/jobs/" in apply_location and job_id not in apply_location

    applied = _wait_for_import(client, cookies, apply_location)
    assert b"Import applied" in applied.content
    db_session.expire_all()
    assert SqlWorkRepository(db_session).get_by_isbn("9780441013799") is not None

    # 3. Applying a NON-dry-run job (the real import we just ran) is refused.
    real_job_id = apply_location.rstrip("/").split("/")[-1]
    raw3, signed3 = _make_csrf_pair()
    refused = client.post(
        f"/ui/admin/import/jobs/{real_job_id}/apply",
        data={"csrf_token": raw3},
        cookies={**cookies, CSRF_COOKIE: signed3},
    )
    assert refused.status_code == 404


def test_web_export_marc_downloads(client, db_session):
    _make_user(db_session, "Librarian", "web_exp_marc")
    db_session.commit()
    cookies = _login(client, "web_exp_marc")
    raw, signed = _make_csrf_pair()
    resp = client.post(
        "/ui/admin/import",
        data={
            "format": "marc",
            "mode": "append",
            "default_media_type": "",
            "default_branch": "",
            "csrf_token": raw,
        },
        files={"file": ("in.mrc", _marc_bytes("9780441013706"), "application/marc")},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    _wait_for_import(client, cookies, resp.headers["location"])

    raw2, signed2 = _make_csrf_pair()
    resp = client.post(
        "/ui/admin/export",
        data={
            "format": "marc",
            "media_type": "",
            "branch": "",
            "since": "",
            "csrf_token": raw2,
        },
        cookies={**cookies, CSRF_COOKIE: signed2},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/marc")
    assert len(resp.content) > 0
