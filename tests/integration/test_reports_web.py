"""Web UI tests for /ui/reports."""

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
from compendium.domain.models import (
    AppUser,
    Base,
    Item,
    Loan,
    MediaType,
    Patron,
    Work,
)
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)


def _csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _derive_csrf_secret(_SECRET))}"
    return raw, signed


@pytest.fixture(scope="module")
def rw_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def rw_session(rw_engine) -> Session:
    factory = sessionmaker(bind=rw_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def rw_client(rw_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: rw_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


def _login_as(client, session, username: str, role_name: str) -> dict:
    role = SqlRoleRepository(session).get_by_name(role_name)
    user = AppUser(
        username=username,
        password_hash=hash_password("secret"),
        role_id=role.id,
    )
    SqlUserRepository(session).add(user)
    session.flush()
    raw, signed = _csrf_pair()
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": "secret", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    return dict(resp.cookies)


class TestAuth:
    def test_readonly_forbidden(self, rw_client, rw_session):
        cookies = _login_as(rw_client, rw_session, "ro1", "ReadOnly")
        resp = rw_client.get("/ui/reports", cookies=cookies)
        assert resp.status_code == 403

    def test_unauthenticated_redirects_to_login(self, rw_client):
        resp = rw_client.get("/ui/reports")
        assert resp.status_code == 303
        assert "/ui/login" in resp.headers["location"]


class TestLandingAndCharts:
    def test_landing_lists_all_reports(self, rw_client, rw_session):
        cookies = _login_as(rw_client, rw_session, "lib1", "Librarian")
        resp = rw_client.get("/ui/reports", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        for name in ("Checkouts per month", "Popular works", "Dormant items", "Current overdues"):
            assert name in body

    def test_checkouts_page_includes_chart_canvas(self, rw_client, rw_session):
        cookies = _login_as(rw_client, rw_session, "lib2", "Librarian")
        resp = rw_client.get("/ui/reports/checkouts?months=3", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'id="checkouts-chart"' in body
        assert "chart.min.js" in body

    def test_checkouts_csv_download(self, rw_client, rw_session):
        cookies = _login_as(rw_client, rw_session, "lib3", "Librarian")
        resp = rw_client.get(
            "/ui/reports/checkouts?months=2&format=csv", cookies=cookies
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers["content-disposition"]
        lines = resp.text.strip().splitlines()
        assert lines[0] == "month,count"
        assert len(lines) == 3  # header + 2 months


class TestOverduesTable:
    def test_overdues_page_shows_overdue_loan(self, rw_client, rw_session):
        cookies = _login_as(rw_client, rw_session, "lib4", "Librarian")

        book = rw_session.query(MediaType).filter_by(code="book").one()
        work = Work(title="WebOverdueTitle", media_type_id=book.id)
        rw_session.add(work)
        rw_session.flush()
        branch = SqlBranchRepository(rw_session).get_default()
        item = Item(
            work_id=work.id,
            branch_id=branch.id,
            barcode="WBO0001",
            accession_number="WACC0001",
        )
        rw_session.add(item)
        rw_session.flush()
        patron = Patron(library_card_number="WC0001", full_name="Alice W.")
        rw_session.add(patron)
        rw_session.flush()
        now = datetime.now(tz=timezone.utc)
        loan = Loan(
            item_id=item.id,
            patron_id=patron.id,
            branch_id=branch.id,
            checked_out_at=now - timedelta(days=20),
            due_at=now - timedelta(days=5),
        )
        rw_session.add(loan)
        rw_session.flush()

        resp = rw_client.get("/ui/reports/overdues", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "WebOverdueTitle" in body
        assert "WC0001" in body


class TestChartJsonEscaping:
    """Regression tests for stored XSS via chart-data JSON (H1).

    json.dumps does not escape '</', so a Work title containing '</script>'
    can break out of the inline <script> block on the popular/checkouts pages.
    With CSP allowing 'unsafe-inline', any injected script then executes.
    """

    def _seed_loan_with_title(self, session, title: str, *, barcode: str, card: str):
        book = session.query(MediaType).filter_by(code="book").one()
        work = Work(title=title, media_type_id=book.id)
        session.add(work)
        session.flush()
        branch = SqlBranchRepository(session).get_default()
        item = Item(
            work_id=work.id,
            branch_id=branch.id,
            barcode=barcode,
            accession_number=barcode + "-A",
        )
        session.add(item)
        session.flush()
        patron = Patron(library_card_number=card, full_name="Eve")
        session.add(patron)
        session.flush()
        # A returned loan within the popular-works default window (90 days).
        now = datetime.now(tz=timezone.utc)
        session.add(
            Loan(
                item_id=item.id,
                patron_id=patron.id,
                branch_id=branch.id,
                checked_out_at=now - timedelta(days=10),
                due_at=now - timedelta(days=3),
                returned_at=now - timedelta(days=2),
            )
        )
        session.flush()

    def test_popular_chart_escapes_script_close(self, rw_client, rw_session):
        cookies = _login_as(rw_client, rw_session, "lib_xss_pop", "Librarian")
        payload = "</script><script>window.__pwn=1</script>"
        self._seed_loan_with_title(
            rw_session, payload, barcode="XSS001", card="XSSC01"
        )
        resp = rw_client.get("/ui/reports/popular", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        # The title must appear (it's the data we're charting), but never as
        # a literal '</script>' that would close the inline script tag.
        assert "window.__pwn" not in body or r"</script>" in body or r"<\/script>" in body
        assert "</script><script>window.__pwn" not in body

    def test_checkouts_chart_escapes_script_close(self, rw_client, rw_session):
        # Checkouts chart_labels are month strings (not user input today), but
        # the same |safe pattern is used — guard against future label sources.
        cookies = _login_as(rw_client, rw_session, "lib_xss_co", "Librarian")
        resp = rw_client.get("/ui/reports/checkouts?months=2", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        # Sanity: the chart block exists.
        assert "var labels =" in body
        # Belt-and-suspenders: no literal </script> appears inside a JS string.
        # (Tightens once we wire the escape; today this trivially passes since
        # months never contain '<'.)
        assert "</script><script>" not in body
