"""First-run checklist: service, card, dismiss route (UX slice 5)."""
from datetime import date, time

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
from compendium.domain.models import (
    AppUser, Base, Branch, ClosedDate, Item, LoanPolicy, MediaType, SiteSetting, Work,
)
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import hash_password
from compendium.services.first_run import first_run_status
import compendium.services.site_settings as ss
from tests.helpers import setup_sqlite_fts
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token

_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)
_CSRF_KEY = _derive_csrf_secret(_SECRET)


def _make_csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _CSRF_KEY)}"
    return raw, signed


@pytest.fixture(scope="module")
def web_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    setup_sqlite_fts(engine)
    return engine


@pytest.fixture
def web_session(web_engine) -> Session:
    factory = sessionmaker(bind=web_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def web_client(web_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: web_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


@pytest.fixture
def admin_user(web_session):
    role = SqlRoleRepository(web_session).get_by_name("Administrator")
    user = AppUser(username="fradmin", password_hash=hash_password("secret"), role_id=role.id)
    SqlUserRepository(web_session).add(user)
    web_session.flush()
    return user


@pytest.fixture
def librarian_user(web_session):
    role = SqlRoleRepository(web_session).get_by_name("Librarian")
    user = AppUser(username="frlib", password_hash=hash_password("secret"), role_id=role.id)
    SqlUserRepository(web_session).add(user)
    web_session.flush()
    return user


def _login(client, username: str, password: str = "secret") -> dict:
    raw, signed = _make_csrf_pair()
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": password, "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    return dict(resp.cookies)


def _add_item(session) -> None:
    mt = session.query(MediaType).filter_by(code="book").one()
    branch = session.query(Branch).first()
    work = Work(title="Seed Work", media_type_id=mt.id)
    session.add(work)
    session.flush()
    session.add(
        Item(
            work_id=work.id,
            branch_id=branch.id,
            barcode="FR0001",
            accession_number="FRA-1",
        )
    )
    session.flush()


# ── Service ───────────────────────────────────────────────────────────────


def test_fresh_seed_all_steps_undone(web_session):
    status = first_run_status(web_session)
    assert [s.done for s in status.steps] == [False] * 5
    assert status.all_done is False
    assert [s.key for s in status.steps] == ["name", "hours", "policy", "item", "email"]


def test_item_step_done_after_first_item(web_session):
    _add_item(web_session)
    status = first_run_status(web_session)
    assert {s.key: s.done for s in status.steps}["item"] is True


def test_name_step_done_after_branch_rename(web_session):
    web_session.query(Branch).filter_by(code="MAIN").one().name = "Oak Street Library"
    web_session.flush()
    assert {s.key: s.done for s in first_run_status(web_session).steps}["name"] is True


def test_name_step_done_via_library_name_env(web_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_LIBRARY_NAME", "Oak Street Library")
    ss.invalidate_cache()
    try:
        assert {s.key: s.done for s in first_run_status(web_session).steps}["name"] is True
    finally:
        ss.invalidate_cache()


def test_hours_step_done_after_closed_date(web_session):
    web_session.add(
        ClosedDate(start_date=date(2026, 12, 25), end_date=date(2026, 12, 25), label="Holiday")
    )
    web_session.flush()
    assert {s.key: s.done for s in first_run_status(web_session).steps}["hours"] is True


def test_policy_step_done_after_second_policy(web_session):
    web_session.add(LoanPolicy(name="DVDs", media_type_id=None, loan_period_days=7, max_renewals=1, is_default=False))
    web_session.flush()
    assert {s.key: s.done for s in first_run_status(web_session).steps}["policy"] is True


def test_email_step_done_via_smtp_env(web_session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SMTP_HOST", "smtp.example.org")
    ss.invalidate_cache()
    try:
        assert {s.key: s.done for s in first_run_status(web_session).steps}["email"] is True
    finally:
        ss.invalidate_cache()


# ── Card rendering ────────────────────────────────────────────────────────


def test_card_shown_to_admin_on_fresh_landing(web_client, web_session, admin_user):
    cookies = _login(web_client, "fradmin")
    resp = web_client.get("/ui/catalog", cookies=cookies)
    assert resp.status_code == 200
    assert b"Getting started" in resp.content
    assert b"Add your first item" in resp.content


def test_card_hidden_from_librarian(web_client, web_session, librarian_user):
    cookies = _login(web_client, "frlib")
    resp = web_client.get("/ui/catalog", cookies=cookies)
    assert b"Getting started" not in resp.content


def test_card_hidden_from_guest(web_client, web_session):
    resp = web_client.get("/ui/catalog")
    assert b"Getting started" not in resp.content


def test_card_hidden_when_searching(web_client, web_session, admin_user):
    cookies = _login(web_client, "fradmin")
    resp = web_client.get("/ui/catalog?q=dune", cookies=cookies)
    assert b"Getting started" not in resp.content


def test_card_marks_done_steps(web_client, web_session, admin_user):
    _add_item(web_session)
    cookies = _login(web_client, "fradmin")
    resp = web_client.get("/ui/catalog", cookies=cookies)
    assert b'data-done="true"' in resp.content


def test_card_hidden_when_dismissed_via_env(web_client, web_session, admin_user, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_FIRST_RUN_DISMISSED", "true")
    ss.invalidate_cache()
    try:
        cookies = _login(web_client, "fradmin")
        resp = web_client.get("/ui/catalog", cookies=cookies)
        assert b"Getting started" not in resp.content
    finally:
        ss.invalidate_cache()


# ── Dismiss route ─────────────────────────────────────────────────────────


def test_dismiss_writes_setting(web_client, web_session, admin_user):
    cookies = _login(web_client, "fradmin")
    raw, signed = _make_csrf_pair()
    cookies[CSRF_COOKIE] = signed
    resp = web_client.post(
        "/ui/first-run/dismiss", data={"csrf_token": raw}, cookies=cookies
    )
    assert resp.status_code == 200
    assert resp.content == b""
    row = web_session.query(SiteSetting).filter_by(key="first_run_dismissed").one()
    assert row.value == "true"


def test_dismiss_forbidden_without_system_manage(web_client, web_session, librarian_user):
    cookies = _login(web_client, "frlib")
    raw, signed = _make_csrf_pair()
    cookies[CSRF_COOKIE] = signed
    resp = web_client.post(
        "/ui/first-run/dismiss", data={"csrf_token": raw}, cookies=cookies
    )
    assert resp.status_code == 403


# ── CLI epilog ────────────────────────────────────────────────────────────


def test_root_help_shows_quickstart():
    from typer.testing import CliRunner
    from compendium.cli.main import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Quickstart:" in result.output
    assert "compendium keygen" in result.output
    assert "compendium serve" in result.output
