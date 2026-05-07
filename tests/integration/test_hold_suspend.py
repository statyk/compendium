"""Service-level tests for hold suspend/resume."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from compendium.domain.enums import HoldStatus, ItemStatus
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import Hold, Patron
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
from compendium.services.holds import HoldService


_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}


def _seed_work_with_copies(session, n_copies=2, isbn="9780441013593"):
    with patch("compendium.services.metadata.lookup_isbn", return_value=_DUNE):
        catalog = CatalogService(
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            creator_repo=SqlCreatorRepository(session),
            branch_repo=SqlBranchRepository(session),
            media_type_repo=SqlMediaTypeRepository(session),
        )
        work, item = catalog.add_from_isbn(isbn)
        items = [item]
        for _ in range(n_copies - 1):
            items.append(catalog.add_item_to_work(work.id))
    session.flush()
    return work, items


def _patron(session, card):
    p = Patron(library_card_number=card, full_name=f"P{card}")
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


def _holds(session) -> HoldService:
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        source="test",
    )


def _circ(session) -> CirculationService:
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
    )


class TestSuspend:
    def test_suspend_sets_field(self, session):
        work, _ = _seed_work_with_copies(session, n_copies=1)
        _patron(session, "SP0001")
        # Check out so the next hold goes WAITING, not immediately AVAILABLE
        _circ(session).checkout(work.items[0].barcode, "SP0001")

        p2 = _patron(session, "SP0002")
        hold = _holds(session).place(work.id, "SP0002")
        assert hold.status == HoldStatus.WAITING.value

        until = date.today() + timedelta(days=14)
        _holds(session).suspend(hold.id, until=until, patron_id=p2.id, reason="vacation")
        assert hold.suspended_until == until
        assert hold.suspended_reason == "vacation"

    def test_suspend_audits(self, session):
        work, _ = _seed_work_with_copies(session, n_copies=1)
        _patron(session, "SP0003")
        _circ(session).checkout(work.items[0].barcode, "SP0003")
        p2 = _patron(session, "SP0004")
        hold = _holds(session).place(work.id, "SP0004")
        _holds(session).suspend(hold.id, until=date.today() + timedelta(days=7))

        entries = AuditService(SqlAuditLogRepository(session)).list(
            entity_type=AuditEntityType.PATRON, entity_id=p2.id, limit=10
        )
        assert any(e.action == AuditAction.HOLD_SUSPEND for e in entries)

    def test_suspend_rejects_past_date(self, session):
        work, _ = _seed_work_with_copies(session, n_copies=1)
        _patron(session, "SP0005")
        _circ(session).checkout(work.items[0].barcode, "SP0005")
        _patron(session, "SP0006")
        hold = _holds(session).place(work.id, "SP0006")
        with pytest.raises(ValidationError):
            _holds(session).suspend(hold.id, until=date.today() - timedelta(days=1))

    def test_suspend_rejects_today(self, session):
        work, _ = _seed_work_with_copies(session, n_copies=1)
        _patron(session, "SP0007")
        _circ(session).checkout(work.items[0].barcode, "SP0007")
        _patron(session, "SP0008")
        hold = _holds(session).place(work.id, "SP0008")
        with pytest.raises(ValidationError):
            _holds(session).suspend(hold.id, until=date.today())

    def test_suspend_rejects_available_hold(self, session):
        # A hold that immediately promoted to AVAILABLE can't be suspended —
        # the copy is on the pickup shelf, pinned.
        work, _ = _seed_work_with_copies(session)
        _patron(session, "SP0009")
        hold = _holds(session).place(work.id, "SP0009")
        assert hold.status == HoldStatus.AVAILABLE.value
        with pytest.raises(BusinessRuleError, match="waiting"):
            _holds(session).suspend(hold.id, until=date.today() + timedelta(days=7))

    def test_suspend_enforces_ownership(self, session):
        work, _ = _seed_work_with_copies(session, n_copies=1)
        _patron(session, "SP0010")
        _circ(session).checkout(work.items[0].barcode, "SP0010")
        p2 = _patron(session, "SP0011")
        hold = _holds(session).place(work.id, "SP0011")
        p3 = _patron(session, "SP0012")
        # Attempt to suspend p2's hold as p3
        with pytest.raises(BusinessRuleError, match="not belong"):
            _holds(session).suspend(
                hold.id, until=date.today() + timedelta(days=7), patron_id=p3.id
            )


class TestQueueSkipsSuspended:
    def test_suspended_hold_is_skipped_during_promotion(self, session):
        # Two patrons both place holds (both WAITING since only 1 copy and it's checked out)
        work, _ = _seed_work_with_copies(session, n_copies=1)
        p1 = _patron(session, "QS0001")
        _circ(session).checkout(work.items[0].barcode, "QS0001")
        # p2 places hold first
        p2 = _patron(session, "QS0002")
        hold2 = _holds(session).place(work.id, "QS0002")
        # p3 places hold second
        p3 = _patron(session, "QS0003")
        hold3 = _holds(session).place(work.id, "QS0003")
        # p2 suspends their hold
        _holds(session).suspend(
            hold2.id, until=date.today() + timedelta(days=30), patron_id=p2.id
        )
        # Item checked in → should promote p3 (skipping suspended p2)
        _circ(session).checkin(work.items[0].barcode)
        session.refresh(hold3)
        assert hold3.status == HoldStatus.AVAILABLE.value
        # p2's hold stays WAITING + still suspended
        session.refresh(hold2)
        assert hold2.status == HoldStatus.WAITING.value
        assert hold2.suspended_until is not None


class TestResume:
    def test_resume_without_available_copy_stays_waiting(self, session):
        work, _ = _seed_work_with_copies(session, n_copies=1)
        _patron(session, "RE0001")
        _circ(session).checkout(work.items[0].barcode, "RE0001")
        p2 = _patron(session, "RE0002")
        hold = _holds(session).place(work.id, "RE0002")
        _holds(session).suspend(hold.id, until=date.today() + timedelta(days=7))
        resumed = _holds(session).resume(hold.id, patron_id=p2.id)
        assert resumed.suspended_until is None
        assert resumed.status == HoldStatus.WAITING.value  # no copy available

    def test_resume_auto_promotes_when_copy_available(self, session):
        # Two-copy work: copy 0 checked out, copy 1 still AVAILABLE.
        # A suspended hold is resumed → should auto-promote onto the AVAILABLE copy.
        work, items = _seed_work_with_copies(session, n_copies=2)
        _patron(session, "RE0010")
        _circ(session).checkout(items[0].barcode, "RE0010")
        # Before resume: p2 places hold which will immediate-promote onto copy[1]
        # To avoid that, let's simulate: manually insert a WAITING+suspended hold.
        p2 = _patron(session, "RE0011")
        branch = SqlBranchRepository(session).get_default()
        now = datetime.now(timezone.utc)
        hold = Hold(
            work_id=work.id,
            patron_id=p2.id,
            branch_id=branch.id,
            status=HoldStatus.WAITING.value,
            placed_at=now - timedelta(days=5),
            expires_at=now + timedelta(days=25),
            suspended_until=date.today() + timedelta(days=7),
        )
        session.add(hold)
        session.flush()

        resumed = _holds(session).resume(hold.id, patron_id=p2.id)
        assert resumed.suspended_until is None
        assert resumed.status == HoldStatus.AVAILABLE.value
        assert resumed.held_item_id is not None
        # The copy is now ON_HOLD
        copy = SqlItemRepository(session).get(resumed.held_item_id)
        assert copy.status == ItemStatus.ON_HOLD.value

    def test_resume_rejects_unsuspended_hold(self, session):
        work, _ = _seed_work_with_copies(session, n_copies=1)
        _patron(session, "RE0020")
        _circ(session).checkout(work.items[0].barcode, "RE0020")
        _patron(session, "RE0021")
        hold = _holds(session).place(work.id, "RE0021")
        with pytest.raises(BusinessRuleError, match="not suspended"):
            _holds(session).resume(hold.id)

    def test_resume_audits(self, session):
        work, _ = _seed_work_with_copies(session, n_copies=1)
        _patron(session, "RE0030")
        _circ(session).checkout(work.items[0].barcode, "RE0030")
        p2 = _patron(session, "RE0031")
        hold = _holds(session).place(work.id, "RE0031")
        _holds(session).suspend(hold.id, until=date.today() + timedelta(days=7))
        _holds(session).resume(hold.id)

        entries = AuditService(SqlAuditLogRepository(session)).list(
            entity_type=AuditEntityType.PATRON, entity_id=p2.id, limit=10
        )
        assert any(e.action == AuditAction.HOLD_RESUME for e in entries)


class TestMaintenanceResume:
    def test_resume_expired_suspends_flips_matching(self, session):
        work, _ = _seed_work_with_copies(session, n_copies=1)
        _patron(session, "MR0001")
        _circ(session).checkout(work.items[0].barcode, "MR0001")
        p2 = _patron(session, "MR0002")
        hold = _holds(session).place(work.id, "MR0002")
        # Suspend until yesterday (pre-set past the cutoff for the test)
        hold.suspended_until = date.today() - timedelta(days=1)
        session.flush()

        resumed = _holds(session).resume_expired_suspends()
        assert any(h.id == hold.id for h in resumed)
        session.refresh(hold)
        assert hold.suspended_until is None

    def test_resume_expired_suspends_dry_run_does_not_change_state(self, session):
        work, _ = _seed_work_with_copies(session, n_copies=1)
        _patron(session, "MR0010")
        _circ(session).checkout(work.items[0].barcode, "MR0010")
        _patron(session, "MR0011")
        hold = _holds(session).place(work.id, "MR0011")
        hold.suspended_until = date.today() - timedelta(days=1)
        session.flush()

        matches = _holds(session).resume_expired_suspends(dry_run=True)
        assert any(h.id == hold.id for h in matches)
        session.refresh(hold)
        assert hold.suspended_until is not None  # still suspended

    def test_resume_expired_ignores_future_dates(self, session):
        work, _ = _seed_work_with_copies(session, n_copies=1)
        _patron(session, "MR0020")
        _circ(session).checkout(work.items[0].barcode, "MR0020")
        _patron(session, "MR0021")
        hold = _holds(session).place(work.id, "MR0021")
        hold.suspended_until = date.today() + timedelta(days=7)  # future
        session.flush()

        resumed = _holds(session).resume_expired_suspends()
        assert all(h.id != hold.id for h in resumed)


class TestMaintenanceResumeCli:
    """CLI-level coverage for `--quiet` on resume-expired-suspends.

    Exercises the same seed pattern as TestMaintenanceResume, but invokes the
    Typer command (with session_scope patched to share the test session) so
    we verify the flag plumbing end-to-end.
    """

    @staticmethod
    def _seed_one_expired_suspend(session):
        work, _ = _seed_work_with_copies(session, n_copies=1, isbn="9780441013700")
        _patron(session, "QR0001")
        _circ(session).checkout(work.items[0].barcode, "QR0001")
        _patron(session, "QR0002")
        hold = _holds(session).place(work.id, "QR0002")
        hold.suspended_until = date.today() - timedelta(days=1)
        session.flush()
        return hold

    @staticmethod
    def _run(session, args):
        from contextlib import contextmanager
        from typer.testing import CliRunner
        from compendium.cli.main import app

        @contextmanager
        def _scope():
            yield session

        runner = CliRunner()
        with patch("compendium.cli.commands.maintenance.session_scope", _scope):
            return runner.invoke(app, args)

    def test_default_includes_per_hold_detail(self, session):
        self._seed_one_expired_suspend(session)
        r = self._run(session, ["maintenance", "resume-expired-suspends", "--dry-run"])
        assert r.exit_code == 0, r.output
        assert "Would resume 1 hold(s):" in r.output
        assert "patron_id=" in r.output  # detail line present

    def test_quiet_suppresses_per_hold_detail(self, session):
        self._seed_one_expired_suspend(session)
        r = self._run(
            session,
            ["maintenance", "resume-expired-suspends", "--dry-run", "--quiet"],
        )
        assert r.exit_code == 0, r.output
        assert "Would resume 1 hold(s)." in r.output  # period, not colon
        assert "patron_id=" not in r.output  # detail line suppressed
