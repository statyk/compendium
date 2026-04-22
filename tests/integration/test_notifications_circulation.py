"""End-to-end test: hold promotion (via checkin) queues a hold_ready notification."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from compendium.config.settings import Settings
from compendium.domain.enums import (
    HoldStatus,
    NotificationStatus,
    NotificationTemplate,
)
from compendium.domain.models import Hold, Patron
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.notification_repository import SqlNotificationRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
from compendium.services.notifications import NotificationService


def _seed(session):
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
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            creator_repo=SqlCreatorRepository(session),
            branch_repo=SqlBranchRepository(session),
            media_type_repo=SqlMediaTypeRepository(session),
        ).add_from_isbn("9780441013593")
    session.flush()
    return work, item


def _patron(session, card, *, email="p@example.test", opt_in=True):
    p = Patron(
        library_card_number=card,
        full_name="Alice",
        contact_email=email,
        receive_notifications=opt_in,
    )
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


def _build(session):
    settings = Settings(database_url="sqlite:///:memory:")
    audit = AuditService(SqlAuditLogRepository(session))
    notif = NotificationService(
        notification_repo=SqlNotificationRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        settings=settings,
        audit_svc=audit,
        source="test",
    )
    circ = CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        notification_svc=notif,
        audit_svc=audit,
        source="test",
    )
    return circ, notif


def _place_waiting_hold(session, patron, work, branch_id):
    hold = Hold(
        work_id=work.id,
        patron_id=patron.id,
        branch_id=branch_id,
        status=HoldStatus.WAITING.value,
        placed_at=datetime.now(timezone.utc),
    )
    SqlHoldRepository(session).add(hold)
    session.flush()
    return hold


def test_checkin_promotes_hold_and_queues_notification(session):
    work, item = _seed(session)
    borrower = _patron(session, "NC0001")
    waiter = _patron(session, "NC0002", email="waiter@example.test")
    circ, notif = _build(session)

    circ.checkout(item.barcode, "NC0001")
    _place_waiting_hold(session, waiter, work, item.branch_id)
    circ.checkin(item.barcode)

    notifications = notif.list()
    assert len(notifications) == 1
    n = notifications[0]
    assert n.template_key == NotificationTemplate.HOLD_READY.value
    assert n.recipient_email == "waiter@example.test"
    assert n.status == NotificationStatus.PENDING.value


def test_checkin_skips_notification_when_waiter_opted_out(session):
    work, item = _seed(session)
    borrower = _patron(session, "NC0003")
    waiter = _patron(session, "NC0004", email="waiter2@example.test", opt_in=False)
    circ, notif = _build(session)

    circ.checkout(item.barcode, "NC0003")
    _place_waiting_hold(session, waiter, work, item.branch_id)
    circ.checkin(item.barcode)

    assert notif.list() == []


def test_checkin_skips_notification_when_waiter_has_no_email(session):
    work, item = _seed(session)
    borrower = _patron(session, "NC0005")
    waiter = _patron(session, "NC0006", email=None)
    circ, notif = _build(session)

    circ.checkout(item.barcode, "NC0005")
    _place_waiting_hold(session, waiter, work, item.branch_id)
    circ.checkin(item.barcode)

    assert notif.list() == []


def test_circulation_works_without_notification_svc(session):
    """Regression: CirculationService still works when no notification_svc wired."""
    work, item = _seed(session)
    _patron(session, "NC0007")
    _patron(session, "NC0008", email="waiter3@example.test")
    circ_no_notif = CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
    )
    circ_no_notif.checkout(item.barcode, "NC0007")
    waiter = SqlPatronRepository(session).get_by_card_number("NC0008")
    _place_waiting_hold(session, waiter, work, item.branch_id)
    # Must not raise
    circ_no_notif.checkin(item.barcode)
