"""Integration tests for per-source metadata refresh.

Covers:
- CatalogService.refresh_metadata(source=...) routes to the named adapter
- Invalid source for a media type raises ValueError and surfaces as error in web/CLI
- Web GET preview passes source via ?source= query param
- Web POST apply passes source via form field
- API query param on refresh endpoints
- CLI --source flag
"""
from __future__ import annotations

import io
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from compendium.api.app import create_app
from compendium.cli.main import app as cli_app
from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Base
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services import site_settings as ss
from compendium.services.audit import AuditService
from compendium.services.auth import AuthService, hash_password
from compendium.services.catalog import CatalogService
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from compendium.web.deps import AUTH_COOKIE

_SETTINGS = Settings(database_url="sqlite:///:memory:")

_META_GB = {
    "title": "Dune",
    "authors": ["Frank Herbert"],
    "creator_role": "author",
    "description": "From Google Books.",
    "isbn": "9780441013593",
    "upc": None,
    "publisher": None,
    "publication_year": None,
    "cover_image_url": None,
    "external_ids": {"google_books": "abc123"},
    "extra_metadata": {},
    "lc_classification": None,
    "ddc_classification": None,
    "lccn": None,
    "subtitle": None,
}

_META_OL = {
    "title": "Dune",
    "authors": ["Frank Herbert"],
    "creator_role": "author",
    "description": "From Open Library.",
    "isbn": "9780441013593",
    "upc": None,
    "publisher": None,
    "publication_year": None,
    "cover_image_url": None,
    "external_ids": {"openlibrary": "OL_dune"},
    "extra_metadata": {},
    "lc_classification": None,
    "ddc_classification": None,
    "lccn": None,
    "subtitle": None,
}


@pytest.fixture
def s_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def s_session(s_engine):
    factory = sessionmaker(bind=s_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture(autouse=True)
def _env_isolation(s_engine, monkeypatch):
    monkeypatch.setattr("compendium.db.engine.get_engine", lambda: s_engine)
    ss.invalidate_cache()
    yield
    ss.invalidate_cache()


def _catalog(session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        source="test",
    )


def _seed_book(session, isbn: str = "9780441013593") -> int:
    """Insert a minimal book Work and return its ID."""
    from compendium.services.import_export import ImportOptions, ImportService

    svc = ImportService(
        session=session,
        catalog=_catalog(session),
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        source="test",
    )
    csv = f"isbn,title,media_type,status,is_loanable\n{isbn},Dune,book,available,true\n"
    with patch("compendium.services.metadata_cache.WriteBuffer.flush"):
        svc.import_csv(io.StringIO(csv), ImportOptions(enrich_from_external=False))
    session.commit()
    works = SqlWorkRepository(session).search("")
    return works[0].id


@pytest.fixture
def client(s_engine, s_session):
    app = create_app()

    def _override():
        factory = sessionmaker(bind=s_engine, autoflush=False, expire_on_commit=False)
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
        with TestClient(app, follow_redirects=False) as c:
            yield c


def _make_admin(s_session) -> tuple[AppUser, str]:
    role = SqlRoleRepository(s_session).get_by_name("Administrator")
    u = AppUser(username="admin", password_hash=hash_password("pw"), role_id=role.id)
    SqlUserRepository(s_session).add(u)
    s_session.commit()
    u.role = role
    return u, AuthService(
        user_repo=SqlUserRepository(s_session),
        role_repo=SqlRoleRepository(s_session),
        settings=_SETTINGS,
    ).issue_token(u)


def _csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _derive_csrf_secret(_SETTINGS.jwt_secret_key))}"
    return raw, signed


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------

def test_refresh_with_source_googlebooks_calls_gb_adapter(s_session):
    """refresh_metadata(source='googlebooks') uses the GB adapter regardless of preference."""
    work_id = _seed_book(s_session)
    svc = _catalog(s_session)

    with (
        patch("compendium.services.metadata.GoogleBooksAdapter.lookup", return_value=dict(_META_GB)),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
        patch("compendium.services.site_settings.get_site_setting", return_value=None),
    ):
        report = svc.refresh_metadata(work_id, dry_run=True, source="googlebooks")

    assert report.found
    assert report.source == "googlebooks"


def test_refresh_with_source_openlibrary_calls_ol_adapter(s_session):
    """refresh_metadata(source='openlibrary') uses the OL adapter regardless of preference."""
    work_id = _seed_book(s_session)
    svc = _catalog(s_session)

    with (
        patch("compendium.services.metadata.OpenLibraryAdapter.lookup", return_value=dict(_META_OL)),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
        patch("compendium.services.site_settings.get_site_setting", return_value=None),
    ):
        report = svc.refresh_metadata(work_id, dry_run=True, source="openlibrary")

    assert report.found
    assert report.source == "openlibrary"


def test_refresh_invalid_source_for_media_type_returns_error(s_session):
    """refresh_metadata(source='googlebooks') on a vinyl work returns an error report (found=False)."""
    from compendium.services.import_export import ImportOptions, ImportService

    svc_import = ImportService(
        session=s_session,
        catalog=_catalog(s_session),
        work_repo=SqlWorkRepository(s_session),
        item_repo=SqlItemRepository(s_session),
        source="test",
    )
    csv = "upc,title,media_type,status,is_loanable\n0036172134310,Kind of Blue,vinyl,available,true\n"
    with patch("compendium.services.metadata_cache.WriteBuffer.flush"):
        svc_import.import_csv(io.StringIO(csv), ImportOptions(enrich_from_external=False))
    s_session.commit()

    works = SqlWorkRepository(s_session).search("")
    vinyl_work = next(w for w in works if w.media_type.code == "vinyl")

    svc = _catalog(s_session)
    # The service catches the ValueError from lookup_metadata_from_source and returns
    # an error RefreshReport rather than raising.
    report = svc.refresh_metadata(vinyl_work.id, dry_run=True, source="googlebooks")
    assert report.found is False
    assert report.error is not None
    assert "googlebooks" in report.error.lower() or "valid" in report.error.lower()


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

def test_web_preview_with_source_googlebooks(client, s_session):
    """GET /catalog/{id}/refresh-metadata?source=googlebooks previews GB result."""
    work_id = _seed_book(s_session)
    _, token = _make_admin(s_session)

    with (
        patch("compendium.services.metadata.GoogleBooksAdapter.lookup", return_value=dict(_META_GB)),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
        patch("compendium.services.site_settings.get_site_setting", return_value=None),
    ):
        resp = client.get(
            f"/ui/catalog/{work_id}/refresh-metadata?source=googlebooks",
            cookies={AUTH_COOKIE: token},
        )

    assert resp.status_code == 200
    assert "googlebooks" in resp.text.lower() or "Google Books" in resp.text


def test_web_preview_with_source_openlibrary(client, s_session):
    """GET /catalog/{id}/refresh-metadata?source=openlibrary previews OL result."""
    work_id = _seed_book(s_session)
    _, token = _make_admin(s_session)

    with (
        patch("compendium.services.metadata.OpenLibraryAdapter.lookup", return_value=dict(_META_OL)),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
        patch("compendium.services.site_settings.get_site_setting", return_value=None),
    ):
        resp = client.get(
            f"/ui/catalog/{work_id}/refresh-metadata?source=openlibrary",
            cookies={AUTH_COOKIE: token},
        )

    assert resp.status_code == 200


def test_web_apply_with_source_openlibrary(client, s_session):
    """POST /catalog/{id}/refresh-metadata with source=openlibrary applies OL data."""
    work_id = _seed_book(s_session)
    _, token = _make_admin(s_session)
    raw_csrf, signed_csrf = _csrf_pair()

    with (
        patch("compendium.services.metadata.OpenLibraryAdapter.lookup", return_value=dict(_META_OL)),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
        patch("compendium.services.site_settings.get_site_setting", return_value=None),
    ):
        resp = client.post(
            f"/ui/catalog/{work_id}/refresh-metadata",
            data={"csrf_token": raw_csrf, "source": "openlibrary"},
            cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed_csrf},
        )

    assert resp.status_code == 303


def test_web_invalid_source_redirects_with_error(client, s_session):
    """Passing an invalid source for a book returns error redirect."""
    work_id = _seed_book(s_session)
    _, token = _make_admin(s_session)
    raw_csrf, signed_csrf = _csrf_pair()

    with patch("compendium.services.metadata_cache.WriteBuffer.flush"):
        resp = client.post(
            f"/ui/catalog/{work_id}/refresh-metadata",
            data={"csrf_token": raw_csrf, "source": "tmdb"},  # not valid for books
            cookies={AUTH_COOKIE: token, CSRF_COOKIE: signed_csrf},
        )

    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_api_preview_source_googlebooks(client, s_session):
    """GET /works/{id}/refresh-metadata?source=googlebooks returns GB result."""
    work_id = _seed_book(s_session)
    _, token = _make_admin(s_session)

    with (
        patch("compendium.services.metadata.GoogleBooksAdapter.lookup", return_value=dict(_META_GB)),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
        patch("compendium.services.site_settings.get_site_setting", return_value=None),
    ):
        resp = client.get(
            f"/works/{work_id}/refresh-metadata?source=googlebooks",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "googlebooks"


def test_api_preview_source_openlibrary(client, s_session):
    """GET /works/{id}/refresh-metadata?source=openlibrary returns OL result."""
    work_id = _seed_book(s_session)
    _, token = _make_admin(s_session)

    with (
        patch("compendium.services.metadata.OpenLibraryAdapter.lookup", return_value=dict(_META_OL)),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
        patch("compendium.services.site_settings.get_site_setting", return_value=None),
    ):
        resp = client.get(
            f"/works/{work_id}/refresh-metadata?source=openlibrary",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json()["source"] == "openlibrary"


def test_api_invalid_source_returns_error_report(client, s_session):
    """GET /works/{id}/refresh-metadata?source=tmdb for a book → found=False with error."""
    work_id = _seed_book(s_session)
    _, token = _make_admin(s_session)

    resp = client.get(
        f"/works/{work_id}/refresh-metadata?source=tmdb",
        headers={"Authorization": f"Bearer {token}"},
    )
    # The catalog service catches invalid-source ValueError and returns an error report.
    assert resp.status_code == 200
    data = resp.json()
    assert data["found"] is False
    assert data["error"] is not None


def test_api_apply_source_openlibrary(client, s_session):
    """POST /works/{id}/refresh-metadata?source=openlibrary commits OL data."""
    work_id = _seed_book(s_session)
    _, token = _make_admin(s_session)

    with (
        patch("compendium.services.metadata.OpenLibraryAdapter.lookup", return_value=dict(_META_OL)),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
        patch("compendium.services.site_settings.get_site_setting", return_value=None),
    ):
        resp = client.post(
            f"/works/{work_id}/refresh-metadata?source=openlibrary",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert resp.json()["source"] == "openlibrary"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_source_openlibrary(s_session):
    """compendium work refresh-metadata --source openlibrary shows OL source."""
    work_id = _seed_book(s_session)

    @contextmanager
    def fake_scope():
        yield s_session
        s_session.commit()

    with (
        patch("compendium.services.metadata.OpenLibraryAdapter.lookup", return_value=dict(_META_OL)),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
        patch("compendium.services.site_settings.get_site_setting", return_value=None),
        patch("compendium.cli.commands.work.session_scope", fake_scope),
    ):
        result = CliRunner().invoke(
            cli_app,
            ["work", "refresh-metadata", "--work-id", str(work_id), "--source", "openlibrary"],
        )

    assert result.exit_code == 0, result.output
    assert "openlibrary" in result.output


def test_cli_source_googlebooks(s_session):
    """compendium work refresh-metadata --source googlebooks shows GB source."""
    work_id = _seed_book(s_session)

    @contextmanager
    def fake_scope():
        yield s_session
        s_session.commit()

    with (
        patch("compendium.services.metadata.GoogleBooksAdapter.lookup", return_value=dict(_META_GB)),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
        patch("compendium.services.site_settings.get_site_setting", return_value=None),
        patch("compendium.cli.commands.work.session_scope", fake_scope),
    ):
        result = CliRunner().invoke(
            cli_app,
            ["work", "refresh-metadata", "--work-id", str(work_id), "--source", "googlebooks"],
        )

    assert result.exit_code == 0, result.output
    assert "googlebooks" in result.output


def test_cli_invalid_source_exits_nonzero(s_session):
    """compendium work refresh-metadata --source tmdb on a book → non-zero exit.

    The catalog service returns an error report (found=False) for an invalid source,
    which the CLI treats as a non-zero exit.
    """
    work_id = _seed_book(s_session)

    @contextmanager
    def fake_scope():
        yield s_session

    with (
        patch("compendium.cli.commands.work.session_scope", fake_scope),
        patch("compendium.services.metadata_cache.WriteBuffer.flush"),
    ):
        result = CliRunner().invoke(
            cli_app,
            ["work", "refresh-metadata", "--work-id", str(work_id), "--source", "tmdb"],
        )

    # The CLI exits non-zero when refresh_metadata reports found=False.
    assert result.exit_code != 0
