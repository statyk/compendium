"""Web UI tests for fines, lost/damaged flows, patron and me pages."""

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
from compendium.domain.enums import FineKind, FineStatus, ItemStatus
from compendium.domain.models import AppUser, Base, Loan, Patron
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import AuthService, hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _sign, generate_token
from tests.helpers import setup_sqlite_fts

_SECRET = "insecure-default-change-in-production"


def _settings_with_block(threshold=None, block_holds=False):
    return Settings(
        database_url="sqlite:///:memory:",
        jwt_secret_key=_SECRET,
        fine_block_threshold_cents=threshold,
        fine_block_holds=block_holds,
    )


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
    base_settings = Settings(database_url="sqlite:///:memory:", jwt_secret_key=_SECRET)
    with patch("compendium.db.engine.get_settings", return_value=base_settings):
        yield TestClient(app, follow_redirects=False)


@pytest.fixture
def client_with_block(engine, db_session):
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
    blocked = _settings_with_block(threshold=100, block_holds=False)
    # Patch every route-module reference that shallow-imported get_settings.
    with patch("compendium.db.engine.get_settings", return_value=blocked), patch(
        "compendium.web.routes.me.get_settings", return_value=blocked
    ), patch("compendium.web.routes.fines.get_settings", return_value=blocked):
        yield TestClient(app, follow_redirects=False)


def _make_csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    signed = f"{raw}.{_sign(raw, _SECRET)}"
    return raw, signed


def _make_user(s, role_name, username):
    role = SqlRoleRepository(s).get_by_name(role_name)
    u = AppUser(username=username, password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(s).add(u)
    s.flush()
    u.role = role
    return u


def _login(client, username):
    raw, signed = _make_csrf_pair()
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": "password", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    return dict(resp.cookies)


def _make_patron(s, card, user=None):
    kwargs = {"library_card_number": card, "full_name": "Alice"}
    if user is not None:
        kwargs["user_id"] = user.id
    p = Patron(**kwargs)
    SqlPatronRepository(s).add(p)
    s.flush()
    return p


def _seed_work_item(s, isbn):
    from compendium.repositories.sql.branch_repository import SqlBranchRepository
    from compendium.repositories.sql.creator_repository import SqlCreatorRepository
    from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
    from compendium.repositories.sql.work_repository import SqlWorkRepository
    from compendium.services.catalog import CatalogService

    with patch(
        "compendium.services.metadata.lookup_isbn",
        return_value={
            "title": "Dune",
            "authors": [{"name": "Frank Herbert"}],
            "publishers": [{"name": "Chilton"}],
            "publish_date": "1965",
            "cover": {},
            "identifiers": {},
        },
    ):
        work, item = CatalogService(
            work_repo=SqlWorkRepository(s),
            item_repo=SqlItemRepository(s),
            creator_repo=SqlCreatorRepository(s),
            branch_repo=SqlBranchRepository(s),
            media_type_repo=SqlMediaTypeRepository(s),
        ).add_from_isbn(isbn)
    s.commit()
    return work, item


def _make_overdue_loan(s, patron, item, days_late=3):
    now = datetime.now(timezone.utc)
    loan = Loan(
        item_id=item.id,
        patron_id=patron.id,
        branch_id=item.branch_id,
        checked_out_at=now - timedelta(days=days_late + 14),
        due_at=now - timedelta(days=days_late),
    )
    SqlLoanRepository(s).add(loan)
    item.status = ItemStatus.CHECKED_OUT.value
    SqlItemRepository(s).update(item)
    s.commit()
    return loan


# ── Librarian patron-fines page ──────────────────────────────────────────────


def test_web_patron_fines_page_renders(client, db_session):
    _make_user(db_session, "Librarian", "web_pf_1")
    _make_patron(db_session, "WEB_PF_0001")
    db_session.commit()
    cookies = _login(client, "web_pf_1")
    resp = client.get("/ui/patrons/WEB_PF_0001/fines", cookies=cookies)
    assert resp.status_code == 200, resp.text
    assert b"Fines" in resp.content
    assert b"$0.00" in resp.content


def test_web_patron_fines_shows_projection_for_overdue(client, db_session):
    _, item = _seed_work_item(db_session, "9780441098001")
    p = _make_patron(db_session, "WEB_PF_0002")
    pol = SqlLoanPolicyRepository(db_session).get_default()
    pol.overdue_fine_per_day_cents = 50
    db_session.flush()
    _make_overdue_loan(db_session, p, item, days_late=3)
    _make_user(db_session, "Librarian", "web_pf_2")
    db_session.commit()

    cookies = _login(client, "web_pf_2")
    resp = client.get("/ui/patrons/WEB_PF_0002/fines", cookies=cookies)
    assert resp.status_code == 200, resp.text
    assert b"Pending overdue" in resp.content
    assert b"$1.50" in resp.content  # 3 days × 50c


def test_web_book_overdue_fines_button(client, db_session):
    _, item = _seed_work_item(db_session, "9780441098002")
    p = _make_patron(db_session, "WEB_PF_0003")
    pol = SqlLoanPolicyRepository(db_session).get_default()
    pol.overdue_fine_per_day_cents = 50
    db_session.flush()
    _make_overdue_loan(db_session, p, item, days_late=3)
    _make_user(db_session, "Librarian", "web_pf_3")
    db_session.commit()

    cookies = _login(client, "web_pf_3")
    raw, signed = _make_csrf_pair()
    resp = client.post(
        "/ui/patrons/WEB_PF_0003/fines/assess-overdue",
        data={"csrf_token": raw},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    # Now Fine row exists
    assert len(SqlFineRepository(db_session).list(patron_id=p.id)) == 1


def test_web_pay_fine_flow(client, db_session):
    p = _make_patron(db_session, "WEB_PF_0004")
    _make_user(db_session, "Librarian", "web_pf_4")
    db_session.commit()
    cookies = _login(client, "web_pf_4")

    # Manually create a fine via service
    from compendium.services.audit import AuditService
    from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
    from compendium.services.fines import FineService

    fs = FineService(
        fine_repo=SqlFineRepository(db_session),
        patron_repo=SqlPatronRepository(db_session),
        loan_repo=SqlLoanRepository(db_session),
        item_repo=SqlItemRepository(db_session),
        policy_repo=SqlLoanPolicyRepository(db_session),
        settings=Settings(database_url="sqlite:///:memory:"),
        audit_svc=AuditService(SqlAuditLogRepository(db_session)),
    )
    fine = fs.assess_manual(p, kind=FineKind.OTHER.value, amount_cents=300, note="x")
    db_session.commit()

    raw, signed = _make_csrf_pair()
    resp = client.post(
        f"/ui/fines/{fine.id}/pay",
        data={"csrf_token": raw, "patron_card": "WEB_PF_0004"},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    db_session.refresh(fine)
    assert fine.status == FineStatus.PAID.value


def test_web_waive_fine_requires_note(client, db_session):
    p = _make_patron(db_session, "WEB_PF_0005")
    _make_user(db_session, "Librarian", "web_pf_5")
    db_session.commit()
    cookies = _login(client, "web_pf_5")

    from compendium.services.audit import AuditService
    from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
    from compendium.services.fines import FineService

    fs = FineService(
        fine_repo=SqlFineRepository(db_session),
        patron_repo=SqlPatronRepository(db_session),
        loan_repo=SqlLoanRepository(db_session),
        item_repo=SqlItemRepository(db_session),
        policy_repo=SqlLoanPolicyRepository(db_session),
        settings=Settings(database_url="sqlite:///:memory:"),
        audit_svc=AuditService(SqlAuditLogRepository(db_session)),
    )
    fine = fs.assess_manual(p, kind=FineKind.OTHER.value, amount_cents=300, note="x")
    db_session.commit()

    raw, signed = _make_csrf_pair()
    resp = client.post(
        f"/ui/fines/{fine.id}/waive",
        data={"csrf_token": raw, "patron_card": "WEB_PF_0005", "note": ""},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303  # redirects to patron fines page with error
    db_session.refresh(fine)
    assert fine.status == FineStatus.OUTSTANDING.value

    raw2, signed2 = _make_csrf_pair()
    resp2 = client.post(
        f"/ui/fines/{fine.id}/waive",
        data={"csrf_token": raw2, "patron_card": "WEB_PF_0005", "note": "goodwill"},
        cookies={**cookies, CSRF_COOKIE: signed2},
    )
    assert resp2.status_code == 303
    db_session.refresh(fine)
    assert fine.status == FineStatus.WAIVED.value


def test_web_patron_fines_forbidden_for_readonly(client, db_session):
    _make_user(db_session, "ReadOnly", "web_pf_ro")
    _make_patron(db_session, "WEB_PF_0006")
    db_session.commit()
    cookies = _login(client, "web_pf_ro")
    resp = client.get("/ui/patrons/WEB_PF_0006/fines", cookies=cookies)
    assert resp.status_code in {302, 303, 403}


# ── Item lost/damaged forms ──────────────────────────────────────────────────


def test_web_declare_lost_flow(client, db_session):
    _, item = _seed_work_item(db_session, "9780441098003")
    p = _make_patron(db_session, "WEB_LD_0001")
    pol = SqlLoanPolicyRepository(db_session).get_default()
    pol.lost_item_default_cents = 1500
    db_session.flush()
    _make_overdue_loan(db_session, p, item, days_late=0)
    _make_user(db_session, "Librarian", "web_ld_1")
    db_session.commit()

    cookies = _login(client, "web_ld_1")
    form = client.get(f"/ui/items/{item.barcode}/lost", cookies=cookies)
    assert form.status_code == 200
    assert b"Declare item lost" in form.content

    raw, signed = _make_csrf_pair()
    submit = client.post(
        f"/ui/items/{item.barcode}/lost",
        data={"csrf_token": raw, "replacement_cost_cents": "", "note": ""},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert submit.status_code == 303
    db_session.refresh(item)
    assert item.status == ItemStatus.LOST.value


def test_web_mark_damaged_flow(client, db_session):
    _, item = _seed_work_item(db_session, "9780441098004")
    p = _make_patron(db_session, "WEB_LD_0002")
    _make_overdue_loan(db_session, p, item, days_late=0)
    _make_user(db_session, "Librarian", "web_ld_2")
    db_session.commit()

    cookies = _login(client, "web_ld_2")
    raw, signed = _make_csrf_pair()
    resp = client.post(
        f"/ui/items/{item.barcode}/damaged",
        data={"csrf_token": raw, "amount_cents": "750", "note": "spine torn"},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    db_session.refresh(item)
    assert item.status == ItemStatus.DAMAGED.value


def test_web_mark_damaged_rejects_missing_note(client, db_session):
    _, item = _seed_work_item(db_session, "9780441098005")
    p = _make_patron(db_session, "WEB_LD_0003")
    _make_overdue_loan(db_session, p, item, days_late=0)
    _make_user(db_session, "Librarian", "web_ld_3")
    db_session.commit()

    cookies = _login(client, "web_ld_3")
    raw, signed = _make_csrf_pair()
    resp = client.post(
        f"/ui/items/{item.barcode}/damaged",
        data={"csrf_token": raw, "amount_cents": "500", "note": ""},
        cookies={**cookies, CSRF_COOKIE: signed},
    )
    assert resp.status_code == 400
    assert b"error-banner" in resp.content


# ── Me/fines self-service ─────────────────────────────────────────────────────


def test_web_me_fines_lists_own_fines(client, db_session):
    role = SqlRoleRepository(db_session).get_by_name("Patron")
    u = AppUser(username="web_me_f", password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(db_session).add(u)
    db_session.flush()
    u.role = role
    p = _make_patron(db_session, "WEB_ME_F01", user=u)

    from compendium.services.audit import AuditService
    from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
    from compendium.services.fines import FineService

    FineService(
        fine_repo=SqlFineRepository(db_session),
        patron_repo=SqlPatronRepository(db_session),
        loan_repo=SqlLoanRepository(db_session),
        item_repo=SqlItemRepository(db_session),
        policy_repo=SqlLoanPolicyRepository(db_session),
        settings=Settings(database_url="sqlite:///:memory:"),
        audit_svc=AuditService(SqlAuditLogRepository(db_session)),
    ).assess_manual(p, kind=FineKind.OTHER.value, amount_cents=250, note="x")
    db_session.commit()

    cookies = _login(client, "web_me_f")
    resp = client.get("/ui/me/fines", cookies=cookies)
    assert resp.status_code == 200
    assert b"$2.50" in resp.content


def test_web_me_holds_shows_pay_at_pickup_warning(client_with_block, db_session):
    role = SqlRoleRepository(db_session).get_by_name("Patron")
    u = AppUser(username="web_me_h", password_hash=hash_password("password"), role_id=role.id)
    SqlUserRepository(db_session).add(u)
    db_session.flush()
    u.role = role
    p = _make_patron(db_session, "WEB_ME_H01", user=u)

    from compendium.services.audit import AuditService
    from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
    from compendium.services.fines import FineService

    FineService(
        fine_repo=SqlFineRepository(db_session),
        patron_repo=SqlPatronRepository(db_session),
        loan_repo=SqlLoanRepository(db_session),
        item_repo=SqlItemRepository(db_session),
        policy_repo=SqlLoanPolicyRepository(db_session),
        settings=_settings_with_block(threshold=100, block_holds=False),
        audit_svc=AuditService(SqlAuditLogRepository(db_session)),
    ).assess_manual(p, kind=FineKind.OTHER.value, amount_cents=500, note="x")
    db_session.commit()

    cookies = _login(client_with_block, "web_me_h")
    resp = client_with_block.get("/ui/me/holds", cookies=cookies)
    assert resp.status_code == 200, resp.text
    # The actual banner text — distinct from the CSS class definition.
    assert b"place holds, but checkout will be blocked at pickup" in resp.content
    assert b"$5.00" in resp.content
