import getpass
from datetime import datetime, timedelta, timezone

import typer

from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.holds import HoldService
from compendium.services.patrons import PatronService

app = typer.Typer(help="Maintenance commands (intended for cron/systemd).")


@app.command("deactivate-expired-patrons")
def deactivate_expired_patrons(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be deactivated without changing data."
    ),
) -> None:
    """Deactivate active patrons whose card expiry date has passed."""
    with session_scope() as session:
        svc = PatronService(
            patron_repo=SqlPatronRepository(session),
            loan_repo=SqlLoanRepository(session),
            hold_repo=SqlHoldRepository(session),
            audit_svc=AuditService(SqlAuditLogRepository(session)),
            actor_label=f"cli:{getpass.getuser()}",
            source="cli",
        )
        matches = svc.deactivate_expired(dry_run=dry_run)
        if not matches:
            typer.echo("No expired patrons to deactivate.")
            return
        verb = "Would deactivate" if dry_run else "Deactivated"
        typer.echo(f"{verb} {len(matches)} patron(s):")
        for p in matches:
            typer.echo(
                f"  {p.library_card_number}  {p.full_name}  expired {p.expires_at.isoformat()}"
            )


def _holds_svc(session) -> HoldService:
    settings = get_settings()
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
        hold_expiry_days=settings.hold_expiry_days,
        hold_pickup_days=settings.hold_pickup_days,
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


@app.command("expire-holds")
def expire_holds() -> None:
    """Expire waiting holds whose expiry date has passed."""
    with session_scope() as session:
        count = _holds_svc(session).expire_holds()
        typer.echo(f"Expired {count} hold(s).")


@app.command("resume-expired-suspends")
def resume_expired_suspends(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be resumed without changing data."
    ),
) -> None:
    """Auto-resume holds whose suspension end-date has passed."""
    with session_scope() as session:
        resumed = _holds_svc(session).resume_expired_suspends(dry_run=dry_run)
        if not resumed:
            typer.echo("No suspended holds are ready to resume.")
            return
        verb = "Would resume" if dry_run else "Resumed"
        typer.echo(f"{verb} {len(resumed)} hold(s):")
        for hold in resumed:
            note = ""
            if hold.status == "available" and not dry_run:
                note = " (immediately promoted)"
            typer.echo(f"  #{hold.id}  work={hold.work_id}  patron_id={hold.patron_id}{note}")


@app.command("prune-audit-log")
def prune_audit_log(
    older_than_days: int | None = typer.Option(
        None, "--older-than-days",
        help="Delete audit rows older than this. Overrides COMPENDIUM_AUDIT_RETENTION_DAYS.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Report what would be deleted without touching the database.",
    ),
) -> None:
    """Delete old audit log rows.

    Takes ``--older-than-days`` or the ``COMPENDIUM_AUDIT_RETENTION_DAYS``
    setting; errors if neither is provided. No default is assumed so that
    sysadmins set the retention window intentionally.
    """
    days = older_than_days if older_than_days is not None else get_settings().audit_retention_days
    if days is None:
        typer.echo(
            "Error: pass --older-than-days N or set COMPENDIUM_AUDIT_RETENTION_DAYS.",
            err=True,
        )
        raise typer.Exit(1)
    if days < 1:
        typer.echo("Error: retention window must be at least 1 day.", err=True)
        raise typer.Exit(1)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope() as session:
        repo = SqlAuditLogRepository(session)
        if dry_run:
            count = repo.count_older_than(cutoff)
            typer.echo(f"Would prune {count} audit row(s) older than {days} day(s).")
            return
        count = repo.delete_older_than(cutoff)
        typer.echo(f"Pruned {count} audit row(s) older than {days} day(s).")


@app.command("assess-overdue-fines")
def assess_overdue_fines_cmd() -> None:
    """Materialize outstanding overdue fines for every currently-overdue loan.

    Idempotent: creates a Fine row per (loan, overdue) or updates the existing
    row's amount. Paid/waived fines are never touched. Safe to run from cron."""
    import getpass

    from compendium.repositories.sql.fine_repository import SqlFineRepository
    from compendium.repositories.sql.item_repository import SqlItemRepository
    from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
    from compendium.repositories.sql.loan_repository import SqlLoanRepository
    from compendium.services.audit import AuditService
    from compendium.services.fines import FineService

    with session_scope() as session:
        settings = get_settings()
        audit = AuditService(SqlAuditLogRepository(session))
        fine_svc = FineService(
            fine_repo=SqlFineRepository(session),
            patron_repo=SqlPatronRepository(session),
            loan_repo=SqlLoanRepository(session),
            item_repo=SqlItemRepository(session),
            policy_repo=SqlLoanPolicyRepository(session),
            settings=settings,
            audit_svc=audit,
            actor_label=f"cli:{getpass.getuser()}",
            source="cli",
        )
        counts = fine_svc.assess_overdue_fines()
    typer.echo(
        f"Overdue fines assessed: "
        f"created={counts['created']}, updated={counts['updated']}, "
        f"unchanged={counts['unchanged']}."
    )


def _make_notification_svc(session):
    import getpass

    from compendium.repositories.sql.hold_repository import SqlHoldRepository
    from compendium.repositories.sql.loan_repository import SqlLoanRepository
    from compendium.repositories.sql.notification_repository import (
        SqlNotificationRepository,
    )
    from compendium.services.audit import AuditService
    from compendium.services.notifications import NotificationService
    from compendium.services.notifications.smtp import SMTPSender

    settings = get_settings()
    return NotificationService(
        notification_repo=SqlNotificationRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        settings=settings,
        sender=SMTPSender(settings),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


@app.command("send-queued-notifications")
def send_queued_notifications_cmd(
    batch_size: int | None = typer.Option(
        None, "--batch-size", help="Overrides COMPENDIUM_NOTIFICATIONS_BATCH_SIZE."
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Drain pending notifications via SMTP (cron-friendly, idempotent)."""
    with session_scope() as session:
        svc = _make_notification_svc(session)
        counts = svc.send_pending(batch_size=batch_size, dry_run=dry_run)
    typer.echo(
        f"Notifications: sent={counts.sent}, failed={counts.failed}, "
        f"cancelled={counts.cancelled}, skipped={counts.skipped}"
        + ("  (dry-run)" if dry_run else "")
    )


@app.command("queue-due-soon-notices")
def queue_due_soon_notices_cmd(
    days_before: int | None = typer.Option(
        None, "--days-before", help="Overrides COMPENDIUM_DUE_SOON_DAYS_BEFORE."
    ),
) -> None:
    """Queue a due-soon reminder for each active loan due within the window."""
    with session_scope() as session:
        svc = _make_notification_svc(session)
        effective = days_before if days_before is not None else get_settings().due_soon_days_before
        counts = svc.queue_due_soon_batch(days_before=effective)
    typer.echo(f"Due-soon notices queued: {counts.queued}")


@app.command("queue-overdue-notices")
def queue_overdue_notices_cmd(
    tiers: str | None = typer.Option(
        None, "--tiers", help="Comma-separated day offsets. Overrides COMPENDIUM_OVERDUE_TIERS."
    ),
) -> None:
    """Queue an overdue notice per active overdue loan at the highest matching tier."""
    raw = tiers if tiers is not None else get_settings().overdue_tiers
    try:
        tier_list = sorted({int(x.strip()) for x in raw.split(",") if x.strip()})
    except ValueError as exc:
        typer.echo(f"Error: tiers must be integers: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not tier_list:
        typer.echo("Error: at least one tier is required.", err=True)
        raise typer.Exit(1)
    with session_scope() as session:
        svc = _make_notification_svc(session)
        counts = svc.queue_overdue_batch(tiers=tier_list)
    typer.echo(f"Overdue notices queued: {counts.queued}")


@app.command("prune-notifications")
def prune_notifications_cmd(
    older_than_days: int | None = typer.Option(
        None,
        "--older-than-days",
        help="Delete rows older than this (default: COMPENDIUM_NOTIFICATION_RETENTION_DAYS).",
    ),
    status: str | None = typer.Option(
        None, "--status", help="pending | sent | failed | cancelled"
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Delete notifications. Specify --older-than-days, --status, or both.

    Age-only prune deletes 'sent' + 'cancelled' rows and preserves 'failed'
    so librarians can triage. Use --status=pending to kill a misfiring queue.
    """
    from datetime import datetime, timedelta, timezone

    days = older_than_days
    if days is None:
        days = get_settings().notification_retention_days
    if days is None and status is None:
        typer.echo(
            "Error: pass --older-than-days N, set COMPENDIUM_NOTIFICATION_RETENTION_DAYS, "
            "or pass --status STATUS.",
            err=True,
        )
        raise typer.Exit(1)
    if days is not None and days < 1:
        typer.echo("Error: retention window must be at least 1 day.", err=True)
        raise typer.Exit(1)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days) if days is not None else None
    )
    if status == "pending" and not dry_run:
        typer.echo(
            "WARNING: --status=pending deletes un-sent queued notifications. "
            "Use --dry-run first to preview.",
            err=True,
        )
    try:
        with session_scope() as session:
            svc = _make_notification_svc(session)
            count = svc.prune(older_than=cutoff, status=status, dry_run=dry_run)
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    verb = "Would delete" if dry_run else "Deleted"
    filter_desc = (
        f"older than {days} day(s)"
        if days is not None
        else ""
    ) + (
        f"{' + ' if days and status else ''}status={status}"
        if status
        else ""
    )
    typer.echo(f"{verb} {count} notification(s) [{filter_desc or 'all'}].")


@app.command("prune-cover-cache")
def prune_cover_cache(
    max_mb: int = typer.Option(
        500, "--max-mb",
        help="Cache size cap in MB. Oldest files (by mtime) are deleted until under cap.",
    ),
) -> None:
    """Evict oldest cover-cache files until total size ≤ --max-mb."""
    if max_mb < 1:
        typer.echo("Error: --max-mb must be at least 1.", err=True)
        raise typer.Exit(1)
    from compendium.services.covers import prune

    removed, freed = prune(max_mb * 1024 * 1024)
    if removed == 0:
        typer.echo(f"Cover cache under {max_mb} MB cap; nothing to prune.")
    else:
        typer.echo(f"Pruned {removed} file(s), freed {freed // 1024} KB.")
