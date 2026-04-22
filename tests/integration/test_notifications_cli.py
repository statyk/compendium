"""CLI integration tests for notifications maintenance commands."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from compendium.cli.commands.maintenance import app as maint_app
from compendium.config.settings import Settings
from compendium.domain.enums import HoldStatus, NotificationStatus
from compendium.domain.models import Hold, Loan, Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.notification_repository import SqlNotificationRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository


def _run(session, args, *, settings=None, sender=None):
    @contextmanager
    def _scope():
        yield session

    s = settings or Settings(database_url="sqlite:///:memory:")
    runner = CliRunner()
    with patch("compendium.cli.commands.maintenance.session_scope", _scope), patch(
        "compendium.cli.commands.maintenance.get_settings", return_value=s
    ):
        if sender is not None:
            with patch(
                "compendium.services.notifications.smtp.SMTPSender",
                return_value=sender,
            ):
                return runner.invoke(maint_app, args)
        return runner.invoke(maint_app, args)


def _make_patron(session, card, *, email="p@example.test", opt_in=True):
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


def test_cli_queue_due_soon_notices(session):
    _, item = _seed_work_item(session)
    p = _make_patron(session, "CLI_DS_01")
    _make_loan(session, p, item, days_until_due=2)
    result = _run(session, ["queue-due-soon-notices", "--days-before", "3"])
    assert result.exit_code == 0, result.output
    assert "queued: 1" in result.output.lower()


def test_cli_queue_overdue_notices(session):
    _, item = _seed_work_item(session)
    p = _make_patron(session, "CLI_OD_01")
    _make_loan(session, p, item, days_until_due=-20)
    result = _run(session, ["queue-overdue-notices", "--tiers", "3,14,30"])
    assert result.exit_code == 0, result.output
    assert "queued: 1" in result.output.lower()


def test_cli_queue_overdue_invalid_tiers(session):
    result = _run(session, ["queue-overdue-notices", "--tiers", "not,numbers"])
    assert result.exit_code == 1
    assert "integers" in (result.stderr + result.output).lower()


def test_cli_send_queued_notifications_without_smtp(session):
    work, item = _seed_work_item(session)
    p = _make_patron(session, "CLI_SEND_01")
    hold = _make_hold(session, p, work, item.branch_id)

    # Queue a hold_ready notification directly
    from compendium.services.notifications import NotificationService

    svc = NotificationService(
        notification_repo=SqlNotificationRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        settings=Settings(database_url="sqlite:///:memory:"),
    )
    svc.queue_hold_ready(hold)

    # SMTP unset → skipped
    result = _run(session, ["send-queued-notifications"])
    assert result.exit_code == 0
    assert "skipped=1" in result.output


def test_cli_send_queued_notifications_with_mocked_sender(session):
    work, item = _seed_work_item(session)
    p = _make_patron(session, "CLI_SEND_02")
    hold = _make_hold(session, p, work, item.branch_id)

    from compendium.services.notifications import NotificationService
    from compendium.services.notifications.smtp import SMTPSender

    svc = NotificationService(
        notification_repo=SqlNotificationRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        settings=Settings(database_url="sqlite:///:memory:"),
    )
    svc.queue_hold_ready(hold)

    fake = MagicMock(spec=SMTPSender)
    fake.is_configured.return_value = True
    settings = Settings(
        database_url="sqlite:///:memory:",
        smtp_host="mail.test",
        smtp_from_address="noreply@example.test",
    )

    @contextmanager
    def _scope():
        yield session

    runner = CliRunner()
    with patch("compendium.cli.commands.maintenance.session_scope", _scope), patch(
        "compendium.cli.commands.maintenance.get_settings", return_value=settings
    ), patch(
        "compendium.services.notifications.smtp.SMTPSender", return_value=fake
    ):
        result = runner.invoke(maint_app, ["send-queued-notifications"])
    assert result.exit_code == 0
    assert "sent=1" in result.output
    fake.send.assert_called_once()


def test_cli_prune_notifications_requires_filter(session):
    result = _run(session, ["prune-notifications"])
    assert result.exit_code == 1
    assert "pass --older-than-days" in (result.stderr + result.output).lower()


def test_cli_prune_notifications_by_status(session):
    work, item = _seed_work_item(session)
    p = _make_patron(session, "CLI_PR_01")
    hold = _make_hold(session, p, work, item.branch_id)

    from compendium.services.notifications import NotificationService

    svc = NotificationService(
        notification_repo=SqlNotificationRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        settings=Settings(database_url="sqlite:///:memory:"),
    )
    svc.queue_hold_ready(hold)

    result = _run(session, ["prune-notifications", "--status", "pending"])
    assert result.exit_code == 0, result.output
    assert "deleted 1" in result.output.lower()


def test_cli_prune_notifications_dry_run_by_status(session):
    work, item = _seed_work_item(session)
    p = _make_patron(session, "CLI_PR_02")
    hold = _make_hold(session, p, work, item.branch_id)

    from compendium.services.notifications import NotificationService

    svc = NotificationService(
        notification_repo=SqlNotificationRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        settings=Settings(database_url="sqlite:///:memory:"),
    )
    svc.queue_hold_ready(hold)

    result = _run(
        session,
        ["prune-notifications", "--status", "sent", "--dry-run"],
    )
    assert result.exit_code == 0
    assert "would delete 0" in result.output.lower()
    # Still present
    assert SqlNotificationRepository(session).list(limit=10)
