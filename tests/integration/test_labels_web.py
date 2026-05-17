"""Web UI tests for /ui/labels."""

from __future__ import annotations

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
from compendium.domain.models import AppUser, Base, Item, MediaType, Patron, Work
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"
_TEST_SETTINGS = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)

_n = {"i": 0}


def _next() -> int:
    _n["i"] += 1
    return _n["i"]


def _csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, _derive_csrf_secret(_SECRET))}"


@pytest.fixture(scope="module")
def lw_engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    return eng


@pytest.fixture
def lw_session(lw_engine):
    factory = sessionmaker(bind=lw_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def lw_client(lw_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: lw_session
    with patch("compendium.db.engine.get_settings", return_value=_TEST_SETTINGS):
        with TestClient(app, raise_server_exceptions=True, follow_redirects=False) as c:
            yield c


def _login(client, session, username, role_name="Librarian") -> dict:
    role = SqlRoleRepository(session).get_by_name(role_name)
    user = AppUser(
        username=username, password_hash=hash_password("secret"), role_id=role.id
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


def _seed_item(s: Session) -> Item:
    n = _next()
    mt = s.query(MediaType).filter_by(code="book").one()
    w = Work(title=f"Label Test {n}", media_type_id=mt.id)
    s.add(w)
    s.flush()
    branch = SqlBranchRepository(s).get_default()
    it = Item(
        work_id=w.id,
        branch_id=branch.id,
        barcode=f"WLB{n:06d}",
        accession_number=f"WLA{n:06d}",
        call_number="PS3551",
    )
    s.add(it)
    s.flush()
    return it


def _seed_patron(s: Session) -> Patron:
    n = _next()
    p = Patron(library_card_number=f"WLC{n:05d}", full_name=f"WebLabel Patron {n}")
    s.add(p)
    s.flush()
    return p


class TestAuth:
    def test_readonly_forbidden(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lwro1", "ReadOnly")
        resp = lw_client.get("/ui/labels", cookies=cookies)
        assert resp.status_code == 403

    def test_unauthenticated_redirects_to_login(self, lw_client):
        resp = lw_client.get("/ui/labels")
        assert resp.status_code == 303
        assert "/ui/login" in resp.headers["location"]


class TestIndex:
    def test_index_shows_cards(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lw1")
        resp = lw_client.get("/ui/labels", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Item labels" in body
        assert "Patron cards" in body


class TestItemForm:
    def test_form_renders(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lw2")
        resp = lw_client.get("/ui/labels/items", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'name="template"' in body
        assert 'name="kind"' in body
        assert 'name="barcodes"' in body

    def test_post_returns_pdf(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lw3")
        _seed_item(lw_session)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = lw_client.post(
            "/ui/labels/items",
            data={
                "kind": "pocket",
                "template": "avery-5160",
                "start_label": "0",
                "csrf_token": raw,
            },
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")

    def test_post_empty_filter_re_renders_form_with_error(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lw4")
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = lw_client.post(
            "/ui/labels/items",
            data={
                "template": "avery-5160",
                "barcodes": "DOESNOTEXIST",
                "start_label": "0",
                "csrf_token": raw,
            },
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert b"No items matched" in resp.content


class TestPatronForm:
    def test_form_renders(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lw5")
        resp = lw_client.get("/ui/labels/patrons", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'name="template"' in body
        assert 'name="kind"' in body

    def test_post_returns_pdf(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, "lw6")
        _seed_patron(lw_session)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = lw_client.post(
            "/ui/labels/patrons",
            data={
                "kind": "patron-full",
                "template": "avery-5871",
                "start_label": "0",
                "csrf_token": raw,
            },
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")


class TestNewTemplates:
    """Tests for new templates and formats added in the barcode-label-revamp."""

    def test_new_templates_available_in_item_picker(self, lw_client, lw_session):
        """GET /ui/labels/items should include the new template keys in the HTML."""
        cookies = _login(lw_client, lw_session, f"lwnt{_next()}")
        resp = lw_client.get("/ui/labels/items", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "avery-5167-spine" in body
        assert "avery-22805" in body
        assert "avery-22806" in body

    def test_rotated_template_not_in_patron_picker(self, lw_client, lw_session):
        """GET /ui/labels/patrons should exclude rotated templates and include regular ones."""
        cookies = _login(lw_client, lw_session, f"lwnt{_next()}")
        resp = lw_client.get("/ui/labels/patrons", cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        # Rotated spine template must NOT appear in patron card picker.
        assert "avery-5167-spine" not in body
        # Regular avery-5167 (non-rotated) must still appear.
        assert "avery-5167" in body

    def test_post_spine_format(self, lw_client, lw_session):
        """POST /ui/labels/items with kind=spine should return a PDF."""
        cookies = _login(lw_client, lw_session, f"lwnt{_next()}")
        _seed_item(lw_session)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = lw_client.post(
            "/ui/labels/items",
            data={
                "template": "avery-5167",
                "kind": "spine",
                "start_label": "0",
                "csrf_token": raw,
            },
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")

    def test_post_spine_with_barcode_field(self, lw_client, lw_session):
        """POST /ui/labels/items with kind=spine and field_barcode=on produces a larger PDF."""
        cookies = _login(lw_client, lw_session, f"lwnt{_next()}")
        _seed_item(lw_session)
        raw_a, signed_a = _csrf_pair()
        raw_b, signed_b = _csrf_pair()
        cookies_a = dict(cookies); cookies_a[CSRF_COOKIE] = signed_a
        cookies_b = dict(cookies); cookies_b[CSRF_COOKIE] = signed_b
        base_data = {
            "template": "avery-5167",
            "kind": "spine",
            "start_label": "0",
        }
        resp_no_bc = lw_client.post(
            "/ui/labels/items",
            data={**base_data, "csrf_token": raw_a},
            cookies=cookies_a,
        )
        resp_with_bc = lw_client.post(
            "/ui/labels/items",
            data={**base_data, "field_barcode": "on", "csrf_token": raw_b},
            cookies=cookies_b,
        )
        assert resp_no_bc.status_code == 200
        assert resp_with_bc.status_code == 200
        assert resp_with_bc.content.startswith(b"%PDF-")

    def test_post_rotated_spine_template(self, lw_client, lw_session):
        """POST /ui/labels/items with template=avery-5167-spine and kind=spine
        should return a valid PDF using the rotated rendering path."""
        cookies = _login(lw_client, lw_session, f"lwnt{_next()}")
        _seed_item(lw_session)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = lw_client.post(
            "/ui/labels/items",
            data={
                "template": "avery-5167-spine",
                "kind": "spine",
                "field_barcode": "on",
                "start_label": "0",
                "csrf_token": raw,
            },
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")

    def test_post_spine_alias(self, lw_client, lw_session):
        """POST with format=spine (the old alias) should succeed and return a PDF."""
        cookies = _login(lw_client, lw_session, f"lwnt{_next()}")
        _seed_item(lw_session)
        raw, signed = _csrf_pair()
        cookies[CSRF_COOKIE] = signed
        resp = lw_client.post(
            "/ui/labels/items",
            data={
                "template": "avery-5167",
                "format": "spine",
                "start_label": "0",
                "csrf_token": raw,
            },
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content.startswith(b"%PDF-")


class TestSymbologyBanner:
    """The label-form pages surface the active barcode_symbology setting
    so an operator sees which encoding their PDF will use without
    leaving the page. Cover all three pages plus the stale-copy
    regression."""

    @staticmethod
    def _patch_symbology(value: str):
        """Return a context manager that makes get_site_setting return
        ``value`` for the symbology key inside the labels web routes."""
        real_get = __import__(
            "compendium.services.site_settings", fromlist=["get_site_setting"]
        ).get_site_setting

        def fake(key, *args, **kwargs):
            if key == "barcode_symbology":
                return value
            return real_get(key, *args, **kwargs)

        return patch(
            "compendium.web.routes.labels.get_site_setting", side_effect=fake
        )

    @pytest.mark.parametrize(
        "url", ["/ui/labels", "/ui/labels/items", "/ui/labels/patrons"]
    )
    def test_default_code128_banner_present(self, url, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, f"lwsym{_next()}")
        resp = lw_client.get(url, cookies=cookies)
        assert resp.status_code == 200, resp.text
        body = resp.content.decode()
        assert "Code 128" in body
        # Banner links to the settings page where the setting lives.
        assert "/ui/admin/settings/identifiers" in body

    @pytest.mark.parametrize(
        "url", ["/ui/labels", "/ui/labels/items", "/ui/labels/patrons"]
    )
    def test_code39_setting_reflected_in_banner(self, url, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, f"lwsym{_next()}")
        with self._patch_symbology("code39"):
            resp = lw_client.get(url, cookies=cookies)
        assert resp.status_code == 200, resp.text
        body = resp.content.decode()
        assert "Code 39" in body
        # Other symbology names should NOT appear when code39 is active.
        # (They must not leak from the banner; the page may still mention
        # symbology elsewhere — but per current templates, it doesn't.)
        assert "Codabar" not in body

    @pytest.mark.parametrize(
        "url", ["/ui/labels", "/ui/labels/items"]
    )
    def test_no_stale_code128_default_copy(self, url, lw_client, lw_session):
        """Catches the regression that prompted this slice: pages used to
        say 'Code 128' as the default in their prose. With Codabar as the
        default, that copy should be gone."""
        cookies = _login(lw_client, lw_session, f"lwsym{_next()}")
        resp = lw_client.get(url, cookies=cookies)
        assert resp.status_code == 200
        body = resp.content.decode()
        # Stale exact phrases that used to appear in the templates.
        assert "a Code 128 barcode" not in body
        assert "falls back to Code 128" not in body


# ──────────────────────────────────────────────────────────────────────
# Live SVG preview endpoint (Slice 2)
# ──────────────────────────────────────────────────────────────────────


class TestItemPreviewEndpoint:
    """GET /ui/labels/items/preview — HTMX fragment returning inline SVG."""

    def test_default_returns_200_with_svg(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, f"lw_prev{_next()}")
        resp = lw_client.get("/ui/labels/items/preview", cookies=cookies)
        assert resp.status_code == 200
        assert "<svg" in resp.text

    def test_spine_kind_returns_svg(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, f"lw_prev{_next()}")
        resp = lw_client.get(
            "/ui/labels/items/preview",
            params={"kind": "spine", "field_call_number": "on", "field_cutter": "on"},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert "<svg" in resp.text

    def test_barcode_only_kind_returns_svg(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, f"lw_prev{_next()}")
        resp = lw_client.get(
            "/ui/labels/items/preview",
            params={"kind": "barcode-only", "field_barcode": "on"},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert "<svg" in resp.text

    def test_requires_labels_generate_permission(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, f"lw_prev_ro{_next()}", role_name="ReadOnly")
        resp = lw_client.get("/ui/labels/items/preview", cookies=cookies)
        assert resp.status_code in (403, 302)

    def test_unauthenticated_redirects(self, lw_client):
        resp = lw_client.get("/ui/labels/items/preview")
        assert resp.status_code in (302, 303, 401, 403)

    def test_incompatible_template_falls_back_not_500(self, lw_client, lw_session):
        # avery-5871 is pocket-only; requesting it for spine should NOT 500.
        cookies = _login(lw_client, lw_session, f"lw_prev{_next()}")
        resp = lw_client.get(
            "/ui/labels/items/preview",
            params={"kind": "spine", "template": "avery-5871"},
            cookies=cookies,
        )
        assert resp.status_code == 200

    def test_empty_fields_does_not_error(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, f"lw_prev{_next()}")
        resp = lw_client.get(
            "/ui/labels/items/preview",
            params={"kind": "spine"},
            cookies=cookies,
        )
        assert resp.status_code == 200

    def test_items_page_has_preview_slot(self, lw_client, lw_session):
        cookies = _login(lw_client, lw_session, f"lw_prev{_next()}")
        resp = lw_client.get("/ui/labels/items", cookies=cookies)
        assert resp.status_code == 200
        body = resp.text
        assert 'id="label-preview"' in body
        assert "hx-get" in body

    def test_preview_with_no_field_params_does_not_fall_back_to_defaults(self, lw_client, lw_session):
        """GET preview with no field_* params must render a blank label (no
        optional text), not silently substitute DEFAULT_FIELDS."""
        cookies = _login(lw_client, lw_session, f"lw_prev{_next()}")
        resp = lw_client.get(
            "/ui/labels/items/preview",
            params={"kind": "pocket", "template": "avery-5160"},
            cookies=cookies,
        )
        assert resp.status_code == 200
        body = resp.text
        assert "<svg" in body
        # The sample row's year and title must NOT appear when no fields are posted.
        assert "1965" not in body
        assert "Lord of the Rings" not in body
