"""Integration tests for AuditLog — verifies that mutating operations record audit entries."""

from unittest.mock import patch

import pytest

from compendium.domain.models import AppUser, LoanPolicy, Patron
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService
from compendium.services.auth import AuthService
from compendium.services.catalog import CatalogService
from compendium.services.patrons import PatronService
from compendium.services.policies import PolicyService

_OPEN_LIB_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}
_ISBN = "9780441013593"


def _actor(session) -> AppUser:
    return SqlUserRepository(session).get_by_username("admin")


def _audit(session) -> AuditService:
    return AuditService(SqlAuditLogRepository(session))


def _audit_entries(session, entity_type=None, action=None):
    entries = SqlAuditLogRepository(session).list(entity_type=entity_type, limit=100)
    if action:
        entries = [e for e in entries if e.action == action]
    return entries


def _catalog(session, actor=None) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=_audit(session),
        actor=actor,
        source="api",
    )


def _patron_svc(session, actor=None) -> PatronService:
    return PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=_audit(session),
        actor=actor,
        source="api",
    )


def _policy_svc(session, actor=None) -> PolicyService:
    return PolicyService(
        policy_repo=SqlLoanPolicyRepository(session),
        audit_svc=_audit(session),
        actor=actor,
        source="api",
    )


# ── Patron ────────────────────────────────────────────────────────────────────

def test_patron_create_records_audit(session):
    patron = _patron_svc(session).create(full_name="Test Patron")

    entries = _audit_entries(session, entity_type=AuditEntityType.PATRON, action=AuditAction.CREATE)
    assert len(entries) == 1
    assert entries[0].entity_id == patron.id
    assert entries[0].details["snapshot"]["name"] == "Test Patron"
    assert entries[0].source == "api"


def test_patron_deactivate_records_audit(session):
    patron = _patron_svc(session).create(full_name="Soon Gone")
    _patron_svc(session).deactivate(patron.library_card_number)

    entries = _audit_entries(session, entity_type=AuditEntityType.PATRON, action=AuditAction.DEACTIVATE)
    assert len(entries) == 1
    assert entries[0].entity_id == patron.id


# ── Policy ────────────────────────────────────────────────────────────────────

def test_policy_create_records_audit(session):
    policy = _policy_svc(session).create(name="Extended", loan_period_days=28, max_renewals=3)

    entries = _audit_entries(session, entity_type=AuditEntityType.POLICY, action=AuditAction.CREATE)
    assert len(entries) == 1
    assert entries[0].entity_id == policy.id
    assert entries[0].details["snapshot"]["loan_period_days"] == 28


def test_policy_update_records_audit(session):
    policy = _policy_svc(session).create(name="Temp", loan_period_days=7, max_renewals=1)
    _policy_svc(session).update(policy.id, loan_period_days=14)

    entries = _audit_entries(session, entity_type=AuditEntityType.POLICY, action=AuditAction.UPDATE)
    assert len(entries) == 1
    assert entries[0].details["before"]["loan_period_days"] == 7
    assert entries[0].details["after"]["loan_period_days"] == 14


# ── Item / Catalog ────────────────────────────────────────────────────────────

@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_add_from_isbn_records_work_and_item_audit(_, session):
    _catalog(session).add_from_isbn(_ISBN)

    work_entries = _audit_entries(session, entity_type=AuditEntityType.WORK, action=AuditAction.CREATE)
    item_entries = _audit_entries(session, entity_type=AuditEntityType.ITEM, action=AuditAction.CREATE)
    assert len(work_entries) == 1
    assert work_entries[0].details["snapshot"]["title"] == "Dune"
    assert len(item_entries) == 1


@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_add_same_isbn_twice_records_only_one_work_audit(_, session):
    _catalog(session).add_from_isbn(_ISBN)
    _catalog(session).add_from_isbn(_ISBN)

    work_entries = _audit_entries(session, entity_type=AuditEntityType.WORK, action=AuditAction.CREATE)
    item_entries = _audit_entries(session, entity_type=AuditEntityType.ITEM, action=AuditAction.CREATE)
    assert len(work_entries) == 1
    assert len(item_entries) == 2


@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_withdraw_item_records_audit(_, session):
    _, item = _catalog(session).add_from_isbn(_ISBN)
    _catalog(session).withdraw_item(item.barcode)

    entries = _audit_entries(session, entity_type=AuditEntityType.ITEM, action=AuditAction.WITHDRAW)
    assert len(entries) == 1
    assert entries[0].entity_id == item.id


# ── User ──────────────────────────────────────────────────────────────────────

def test_user_deactivate_records_audit(session):
    from compendium.db.engine import get_settings

    svc = AuthService(
        user_repo=SqlUserRepository(session),
        role_repo=SqlRoleRepository(session),
        settings=get_settings(),
        audit_svc=_audit(session),
        source="api",
    )
    svc.create_user("newuser", "password123", "ReadOnly")
    svc.deactivate_user("newuser")

    entries = _audit_entries(session, entity_type=AuditEntityType.USER, action=AuditAction.DEACTIVATE)
    assert len(entries) == 1
    assert entries[0].details["snapshot"]["username"] == "newuser"


# ── No-audit pass-through ─────────────────────────────────────────────────────

def test_patron_service_without_audit_svc_still_works(session):
    svc = PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
    )
    patron = svc.create(full_name="No Audit")
    assert patron.id is not None
    assert _audit_entries(session, entity_type=AuditEntityType.PATRON) == []
