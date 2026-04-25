"""NotificationService: queue, render, drain, retry, prune."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from compendium.config.settings import Settings
from compendium.domain.enums import (
    HoldStatus,
    NotificationStatus,
    NotificationTemplate,
)
from compendium.domain.errors import NotFoundError, ValidationError
from compendium.domain.models import Hold, Loan, Patron
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.notification_repository import SqlNotificationRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditAction, AuditService
from compendium.services.catalog import CatalogService
from compendium.services.notifications import NotificationService
from compendium.services.notifications.smtp import SMTPSender


def _settings(host=None, from_addr=None):
    return Settings(
        database_url="sqlite:///:memory:",
        smtp_host=host,
        smtp_from_address=from_addr or ("noreply@example.test" if host else None),
        smtp_from_name="Testlib",
    )


def _build(session, settings=None, sender=None):
    audit = AuditService(SqlAuditLogRepository(session))
    svc = NotificationService(
        notification_repo=SqlNotificationRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        settings=settings or _settings(),
        sender=sender,
        audit_svc=audit,
        source="test",
    )
    return svc, audit


def _make_patron(session, card, *, email="patron@example.test", opt_in=True):
    p = Patron(
        library_card_number=card,
        full_name="Alice",
        contact_email=email,
        receive_notifications=opt_in,
    )
    SqlPatronRepository(session).add(p)
    session.flush()
    return p


def _seed_work_item(session, isbn="9780441013593"):
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
        w, i = CatalogService(
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            creator_repo=SqlCreatorRepository(session),
            branch_repo=SqlBranchRepository(session),
            media_type_repo=SqlMediaTypeRepository(session),
        ).add_from_isbn(isbn)
    session.flush()
    return w, i


def _make_loan(session, patron, item, days_until_due=7):
    now = datetime.now(timezone.utc)
    loan = Loan(
        item_id=item.id,
        patron_id=patron.id,
        branch_id=item.branch_id,
        checked_out_at=now - timedelta(days=1),
        due_at=now + timedelta(days=days_until_due),
    )
    SqlLoanRepository(session).add(loan)
    session.flush()
    return loan


def _make_hold(session, patron, work, branch_id):
    hold = Hold(
        work_id=work.id,
        patron_id=patron.id,
        branch_id=branch_id,
        status=HoldStatus.AVAILABLE.value,
        placed_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    SqlHoldRepository(session).add(hold)
    session.flush()
    return hold


# ── Queuing ──────────────────────────────────────────────────────────────────


def test_queue_hold_ready_creates_notification(session):
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "N0001")
    hold = _make_hold(session, patron, work, item.branch_id)
    svc, _ = _build(session)

    n = svc.queue_hold_ready(hold)
    assert n is not None
    assert n.template_key == NotificationTemplate.HOLD_READY.value
    assert n.status == NotificationStatus.PENDING.value
    assert "Dune" in n.subject
    assert patron.full_name in n.body


def test_queue_hold_ready_dedups(session):
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "N0002")
    hold = _make_hold(session, patron, work, item.branch_id)
    svc, _ = _build(session)

    first = svc.queue_hold_ready(hold)
    second = svc.queue_hold_ready(hold)
    assert first is not None
    assert second is None


def test_queue_skips_patron_without_email(session):
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "N0003", email=None)
    hold = _make_hold(session, patron, work, item.branch_id)
    svc, _ = _build(session)
    assert svc.queue_hold_ready(hold) is None


def test_queue_skips_patron_opted_out(session):
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "N0004", opt_in=False)
    hold = _make_hold(session, patron, work, item.branch_id)
    svc, _ = _build(session)
    assert svc.queue_hold_ready(hold) is None


def test_queue_due_soon(session):
    _, item = _seed_work_item(session)
    patron = _make_patron(session, "N0005")
    loan = _make_loan(session, patron, item, days_until_due=3)
    svc, _ = _build(session)
    n = svc.queue_due_soon(loan)
    assert n is not None
    assert "due soon" in n.subject.lower()


def test_queue_due_soon_dedups_per_renewal_cycle(session):
    _, item = _seed_work_item(session)
    patron = _make_patron(session, "N0006")
    loan = _make_loan(session, patron, item, days_until_due=3)
    svc, _ = _build(session)

    first = svc.queue_due_soon(loan)
    second = svc.queue_due_soon(loan)
    assert first is not None
    assert second is None

    # Simulate renewal: renewal_count increments, new due_soon allowed
    loan.renewal_count += 1
    session.flush()
    third = svc.queue_due_soon(loan)
    assert third is not None
    assert third.discriminator == 1


def test_queue_overdue_tier_uniqueness(session):
    _, item = _seed_work_item(session)
    patron = _make_patron(session, "N0007")
    loan = _make_loan(session, patron, item, days_until_due=-5)  # overdue
    svc, _ = _build(session)

    n1 = svc.queue_overdue(loan, tier=1)
    n2 = svc.queue_overdue(loan, tier=1)
    assert n1 is not None
    assert n2 is None

    n3 = svc.queue_overdue(loan, tier=2)
    assert n3 is not None
    assert n3.discriminator == 2


# ── Batch ────────────────────────────────────────────────────────────────────


def test_queue_due_soon_batch_picks_loans_in_window(session):
    _, item1 = _seed_work_item(session, "9780000000101")
    _, item2 = _seed_work_item(session, "9780000000102")
    p1 = _make_patron(session, "NB0001")
    p2 = _make_patron(session, "NB0002")
    _make_loan(session, p1, item1, days_until_due=2)  # in window
    _make_loan(session, p2, item2, days_until_due=10)  # outside window
    svc, _ = _build(session)

    counts = svc.queue_due_soon_batch(days_before=3)
    assert counts.queued == 1


def test_queue_overdue_batch_picks_highest_matching_tier(session):
    _, item = _seed_work_item(session, "9780000000103")
    p = _make_patron(session, "NB0003")
    _make_loan(session, p, item, days_until_due=-20)  # 20 days late
    svc, _ = _build(session)

    counts = svc.queue_overdue_batch(tiers=[3, 14, 30])
    # Highest matching is tier=14 (not 30 yet)
    assert counts.queued == 1
    rows = svc.list()
    assert rows[0].discriminator == 14


# ── Drainer ──────────────────────────────────────────────────────────────────


def test_send_pending_with_mock_sender_marks_sent(session):
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "ND0001")
    hold = _make_hold(session, patron, work, item.branch_id)

    fake_sender = MagicMock(spec=SMTPSender)
    fake_sender.is_configured.return_value = True
    svc, audit = _build(
        session,
        settings=_settings(host="mail.test"),
        sender=fake_sender,
    )
    svc.queue_hold_ready(hold)
    counts = svc.send_pending()
    assert counts.sent == 1
    assert counts.failed == 0
    fake_sender.send.assert_called_once()

    # Audit summary recorded
    entries = audit.list(entity_type="notification", limit=10)
    assert any(e.action == AuditAction.SEND_NOTIFICATIONS for e in entries)


def test_send_pending_inert_without_smtp(session):
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "ND0002")
    hold = _make_hold(session, patron, work, item.branch_id)

    svc, _ = _build(session)  # no sender, no SMTP config
    svc.queue_hold_ready(hold)
    counts = svc.send_pending()
    assert counts.sent == 0
    assert counts.skipped == 1


def test_send_pending_retries_after_transient_failure(session):
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "ND0003")
    hold = _make_hold(session, patron, work, item.branch_id)

    fake_sender = MagicMock(spec=SMTPSender)
    fake_sender.is_configured.return_value = True
    fake_sender.send.side_effect = RuntimeError("connection refused")
    svc, _ = _build(
        session, settings=_settings(host="mail.test"), sender=fake_sender
    )
    svc.queue_hold_ready(hold)
    svc.send_pending()

    row = svc.list()[0]
    assert row.status == NotificationStatus.PENDING.value
    assert row.attempts == 1
    assert "connection refused" in row.last_error


def test_send_pending_gives_up_at_max_attempts(session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_NOTIFICATIONS_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("COMPENDIUM_SMTP_HOST", "mail.test")
    monkeypatch.setenv("COMPENDIUM_SMTP_FROM_ADDRESS", "noreply@example.test")
    from compendium.services import site_settings as _ss
    _ss.invalidate_cache()
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "ND0004")
    hold = _make_hold(session, patron, work, item.branch_id)

    fake_sender = MagicMock(spec=SMTPSender)
    fake_sender.is_configured.return_value = True
    fake_sender.send.side_effect = RuntimeError("auth failed")
    svc, _ = _build(session, sender=fake_sender)
    svc.queue_hold_ready(hold)
    svc.send_pending()
    svc.send_pending()
    row = svc.list()[0]
    assert row.status == NotificationStatus.FAILED.value
    assert row.attempts == 2


def test_send_pending_cancels_missing_email(session):
    # A notification row with recipient_email=None (shouldn't normally happen,
    # but defend against the path).
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "ND0005")
    hold = _make_hold(session, patron, work, item.branch_id)
    svc, _ = _build(session, settings=_settings(host="mail.test"), sender=MagicMock(spec=SMTPSender, **{"is_configured.return_value": True}))
    n = svc.queue_hold_ready(hold)
    n.recipient_email = None
    SqlNotificationRepository(session).update(n)
    counts = svc.send_pending()
    assert counts.cancelled == 1


def test_send_pending_dry_run_marks_skipped_no_writes(session):
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "ND0006")
    hold = _make_hold(session, patron, work, item.branch_id)
    svc, _ = _build(session, settings=_settings(host="mail.test"), sender=MagicMock(spec=SMTPSender, **{"is_configured.return_value": True}))
    svc.queue_hold_ready(hold)
    counts = svc.send_pending(dry_run=True)
    assert counts.skipped == 1
    row = svc.list()[0]
    assert row.status == NotificationStatus.PENDING.value


# ── Retry ─────────────────────────────────────────────────────────────────────


def test_retry_resets_failed_to_pending(session):
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "NR0001")
    hold = _make_hold(session, patron, work, item.branch_id)
    svc, audit = _build(session)
    n = svc.queue_hold_ready(hold)
    n.status = NotificationStatus.FAILED.value
    n.attempts = 5
    n.last_error = "whatever"
    SqlNotificationRepository(session).update(n)

    retried = svc.retry(n.id)
    assert retried.status == NotificationStatus.PENDING.value
    assert retried.attempts == 0
    assert retried.last_error is None
    entries = audit.list(entity_type="notification", entity_id=n.id)
    assert any(e.action == AuditAction.RETRY_NOTIFICATION for e in entries)


def test_retry_rejects_sent_row(session):
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "NR0002")
    hold = _make_hold(session, patron, work, item.branch_id)
    svc, _ = _build(session)
    n = svc.queue_hold_ready(hold)
    n.status = NotificationStatus.SENT.value
    SqlNotificationRepository(session).update(n)
    with pytest.raises(ValidationError):
        svc.retry(n.id)


def test_retry_unknown_id_raises_not_found(session):
    svc, _ = _build(session)
    with pytest.raises(NotFoundError):
        svc.retry(99999)


# ── Prune ─────────────────────────────────────────────────────────────────────


def test_prune_requires_filter(session):
    svc, _ = _build(session)
    with pytest.raises(ValidationError):
        svc.prune()


def test_prune_by_age_preserves_failed(session):
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "NP0001")
    hold = _make_hold(session, patron, work, item.branch_id)
    svc, _ = _build(session)

    n = svc.queue_hold_ready(hold)
    # Mark as sent, predate created_at so the age filter matches.
    n.status = NotificationStatus.SENT.value
    n.created_at = datetime.now(timezone.utc) - timedelta(days=100)
    SqlNotificationRepository(session).update(n)

    # A second notification that's old but failed — must survive.
    loan = _make_loan(session, patron, item, days_until_due=-20)
    m = svc.queue_overdue(loan, tier=1)
    m.status = NotificationStatus.FAILED.value
    m.created_at = datetime.now(timezone.utc) - timedelta(days=100)
    SqlNotificationRepository(session).update(m)

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted = svc.prune(older_than=cutoff)
    assert deleted == 1  # only the sent row
    remaining = svc.list(limit=10)
    statuses = {r.status for r in remaining}
    assert NotificationStatus.FAILED.value in statuses
    assert NotificationStatus.SENT.value not in statuses


def test_prune_by_status_pending_kills_queue(session):
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "NP0002")
    hold = _make_hold(session, patron, work, item.branch_id)
    svc, _ = _build(session)
    svc.queue_hold_ready(hold)
    deleted = svc.prune(status=NotificationStatus.PENDING.value)
    assert deleted == 1


def test_prune_dry_run_does_not_delete(session):
    work, item = _seed_work_item(session)
    patron = _make_patron(session, "NP0003")
    hold = _make_hold(session, patron, work, item.branch_id)
    svc, _ = _build(session)
    svc.queue_hold_ready(hold)
    count = svc.prune(status=NotificationStatus.PENDING.value, dry_run=True)
    assert count == 1
    assert len(svc.list()) == 1


def test_prune_invalid_status_raises(session):
    svc, _ = _build(session)
    with pytest.raises(ValidationError):
        svc.prune(status="bogus")
