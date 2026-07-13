import getpass
from datetime import datetime, timedelta, timezone

import typer

from compendium.services.site_settings import get_site_setting
from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.failed_login_repository import SqlFailedLoginRepository
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
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help="Suppress the per-patron list. Count summary still prints.",
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
        if quiet:
            typer.echo(f"{verb} {len(matches)} patron(s).")
            return
        typer.echo(f"{verb} {len(matches)} patron(s):")
        for p in matches:
            typer.echo(
                f"  {p.library_card_number}  {p.full_name}  expired {p.expires_at.isoformat()}"
            )


def _calendar_svc(session):
    from compendium.repositories.sql.calendar_repository import (
        SqlClosedDateRepository,
        SqlLibraryHoursRepository,
    )
    from compendium.services.calendar import CalendarService

    return CalendarService(
        hours_repo=SqlLibraryHoursRepository(session),
        closed_date_repo=SqlClosedDateRepository(session),
        timezone=get_site_setting("library_timezone"),
        source="cli",
    )


def _holds_svc(session) -> HoldService:
    settings = get_settings()
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
        hold_expiry_days=get_site_setting("hold_expiry_days"),
        hold_pickup_days=get_site_setting("hold_pickup_days"),
        calendar_svc=_calendar_svc(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


@app.command("expire-holds")
def expire_holds(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be expired without changing data."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress output when there is nothing to do.",
    ),
) -> None:
    """Expire waiting holds whose expiry date has passed."""
    with session_scope() as session:
        count = _holds_svc(session).expire_holds(dry_run=dry_run)
    if count == 0 and quiet:
        return
    verb = "Would expire" if dry_run else "Expired"
    typer.echo(f"{verb} {count} hold(s).")


@app.command("resume-expired-suspends")
def resume_expired_suspends(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be resumed without changing data."
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help="Suppress the per-hold list. Count summary still prints.",
    ),
) -> None:
    """Auto-resume holds whose suspension end-date has passed."""
    with session_scope() as session:
        resumed = _holds_svc(session).resume_expired_suspends(dry_run=dry_run)
        if not resumed:
            typer.echo("No suspended holds are ready to resume.")
            return
        verb = "Would resume" if dry_run else "Resumed"
        if quiet:
            typer.echo(f"{verb} {len(resumed)} hold(s).")
            return
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
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress output when there is nothing to do.",
    ),
) -> None:
    """Delete old audit log rows.

    Takes ``--older-than-days`` or the ``COMPENDIUM_AUDIT_RETENTION_DAYS``
    setting; errors if neither is provided. No default is assumed so that
    sysadmins set the retention window intentionally.
    """
    days = older_than_days if older_than_days is not None else get_site_setting("audit_retention_days")
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
            if count == 0 and quiet:
                return
            typer.echo(f"Would prune {count} audit row(s) older than {days} day(s).")
            return
        count = repo.delete_older_than(cutoff)
    if count == 0 and quiet:
        return
    typer.echo(f"Pruned {count} audit row(s) older than {days} day(s).")


@app.command("prune-scan-pairings")
def prune_scan_pairings(
    older_than_days: int = typer.Option(
        ..., "--older-than-days",
        help="Delete terminal scan-pairing rows older than this many days.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Report what would be deleted without touching the database.",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress output when there is nothing to do.",
    ),
) -> None:
    """Delete old terminal scan-pairing rows.

    Scan pairings are ephemeral: a claim secret is valid for roughly 2 minutes,
    and the resulting phone session lasts ``scan_session_minutes`` (default 60).
    Once a pairing expires or is revoked it can never be used again, so terminal
    rows can be pruned aggressively -- a daily or weekly cadence is fine.

    Only pairings that are no longer usable are pruned: rows where
    ``expires_at`` is older than the cutoff, or where ``revoked_at`` is set and
    older than the cutoff.  Live, unexpired sessions are never touched.

    Suggested cron cadence: daily.
    """
    from compendium.repositories.sql.scan_event_repository import (
        SqlScanEventRepository,
    )
    from compendium.repositories.sql.scan_pairing_repository import (
        SqlScanPairingRepository,
    )
    from compendium.repositories.sql.scan_pending_item_repository import (
        SqlScanPendingItemRepository,
    )

    if older_than_days < 1:
        typer.echo("Error: --older-than-days must be at least 1.", err=True)
        raise typer.Exit(1)

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    with session_scope() as session:
        repo = SqlScanPairingRepository(session)
        ids = repo.terminal_deletable_ids(cutoff)
        if dry_run:
            if len(ids) == 0 and quiet:
                return
            typer.echo(
                f"Would prune {len(ids)} scan-pairing row(s) older than "
                f"{older_than_days} day(s)."
            )
            return
        SqlScanEventRepository(session).delete_for_pairings(ids)
        # Delete ALL pending children of the pairings being removed (all are
        # resolved by construction — terminal_deletable_ids excludes any pairing
        # with a status="pending" row). Mirrors the event repo's cascade so no
        # pending row is left orphaned regardless of its resolved_at.
        SqlScanPendingItemRepository(session).delete_for_pairings(ids)
        # Separately sweep old resolved pending rows on pairings NOT being
        # deleted this run.
        SqlScanPendingItemRepository(session).delete_resolved_older_than(cutoff)
        count = repo.delete_by_ids(ids)
    if count == 0 and quiet:
        return
    typer.echo(
        f"Pruned {count} scan-pairing row(s) older than {older_than_days} day(s)."
    )


@app.command("prune-failed-logins")
def prune_failed_logins(
    older_than_days: int = typer.Option(
        ..., "--older-than-days",
        help="Delete failed-login rows older than this many days.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Report what would be deleted without touching the database.",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress output when there is nothing to do.",
    ),
) -> None:
    """Delete old failed-login rows.

    Keeps the failed_login table from growing unboundedly.  The sliding-window
    throttle only looks back ``login_failure_window_seconds`` (default 300 s),
    so rows older than a day are already stale for throttling purposes.
    Suggested cron cadence: weekly.
    """
    if older_than_days < 1:
        typer.echo("Error: --older-than-days must be at least 1.", err=True)
        raise typer.Exit(1)

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    with session_scope() as session:
        repo = SqlFailedLoginRepository(session)
        if dry_run:
            count = repo.count_older_than(cutoff)
            if count == 0 and quiet:
                return
            typer.echo(f"Would prune {count} failed-login row(s) older than {older_than_days} day(s).")
            return
        count = repo.delete_older_than(cutoff)
    if count == 0 and quiet:
        return
    typer.echo(f"Pruned {count} failed-login row(s) older than {older_than_days} day(s).")


@app.command("assess-overdue-fines")
def assess_overdue_fines_cmd(
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress output when there is nothing to do.",
    ),
) -> None:
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
            calendar_svc=_calendar_svc(session),
            audit_svc=audit,
            actor_label=f"cli:{getpass.getuser()}",
            source="cli",
        )
        counts = fine_svc.assess_overdue_fines()
    if counts["created"] == 0 and counts["updated"] == 0 and quiet:
        return
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
        sender=SMTPSender(),
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
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress output when there is nothing to do.",
    ),
) -> None:
    """Drain pending notifications via SMTP (cron-friendly, idempotent)."""
    with session_scope() as session:
        svc = _make_notification_svc(session)
        counts = svc.send_pending(batch_size=batch_size, dry_run=dry_run)
    if counts.sent == 0 and counts.failed == 0 and counts.cancelled == 0 and counts.skipped == 0 and quiet:
        return
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
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress output when there is nothing to do.",
    ),
) -> None:
    """Queue a due-soon reminder for each active loan due within the window."""
    with session_scope() as session:
        svc = _make_notification_svc(session)
        effective = days_before if days_before is not None else get_site_setting("due_soon_days_before")
        counts = svc.queue_due_soon_batch(days_before=effective)
    if counts.queued == 0 and quiet:
        return
    typer.echo(f"Due-soon notices queued: {counts.queued}")


@app.command("queue-overdue-notices")
def queue_overdue_notices_cmd(
    tiers: str | None = typer.Option(
        None, "--tiers", help="Comma-separated day offsets. Overrides COMPENDIUM_OVERDUE_TIERS."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress output when there is nothing to do.",
    ),
) -> None:
    """Queue an overdue notice per active overdue loan at the highest matching tier."""
    if tiers is not None:
        try:
            tier_list = sorted(
                {int(x.strip()) for x in tiers.split(",") if x.strip()}
            )
        except ValueError as exc:
            typer.echo(f"Error: tiers must be integers: {exc}", err=True)
            raise typer.Exit(1) from exc
    else:
        tier_list = sorted(set(get_site_setting("overdue_tiers")))
    if not tier_list:
        typer.echo("Error: at least one tier is required.", err=True)
        raise typer.Exit(1)
    with session_scope() as session:
        svc = _make_notification_svc(session)
        counts = svc.queue_overdue_batch(tiers=tier_list)
    if counts.queued == 0 and quiet:
        return
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
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress output when there is nothing to do.",
    ),
) -> None:
    """Delete notifications. Specify --older-than-days, --status, or both.

    Age-only prune deletes 'sent' + 'cancelled' rows and preserves 'failed'
    so librarians can triage. Use --status=pending to kill a misfiring queue.
    """
    from datetime import datetime, timedelta, timezone

    days = older_than_days
    if days is None:
        days = get_site_setting("notification_retention_days")
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
    if count == 0 and quiet:
        return
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


def _format_progress_line(
    index: int,
    total: int,
    work,
    per,
) -> str:
    """One-line progress entry for refresh-metadata. See refresh_metadata_cmd."""
    width = max(2, len(str(total)))
    counter = f"[{index:>{width}}/{total}]"
    identifier = work.isbn or work.upc or f"id={work.id}"
    label = f"{work.title} ({identifier})"
    if per is None:
        return f"{counter} errored:   {label} — refresh raised an exception"
    if per.error:
        verb = (
            "skipped"
            if ("no ISBN/UPC" in per.error or "no media type" in per.error)
            else "not found"
        )
        return f"{counter} {verb}: {label} — {per.error}"
    if not per.found:
        return f"{counter} not found: {label}"
    if per.planned:
        fields = ", ".join(f"+{name}" for name in sorted(per.planned.keys()))
        return f"{counter} refreshed: {label} [{fields}]"
    return f"{counter} no change: {label}"


@app.command("refresh-metadata")
def refresh_metadata_cmd(
    media_type: str | None = typer.Option(
        None, "--media-type", help="Restrict to a single media_type code."
    ),
    branch: str | None = typer.Option(
        None, "--branch", help="Restrict to Works with at least one copy in this branch."
    ),
    missing_only: bool = typer.Option(
        True,
        "--missing-only/--all",
        help=(
            "Default: only Works missing core fields (description, cover, "
            "publisher, language). --all re-fetches every Work with a "
            "lookup key."
        ),
    ),
    limit: int | None = typer.Option(
        None, "--limit", help="Stop after this many Works processed."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview planned changes without writing."
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help=(
            "Suppress the per-Work progress line. Errored lines and the "
            "end-of-run summary still print."
        ),
    ),
) -> None:
    """Bulk-fill missing metadata from external sources for existing Works.

    Iterates Works with an ISBN/UPC and (by default) at least one missing
    core field, calls the appropriate external adapter per Work
    (Google Books / Open Library / MusicBrainz / TMDb), and applies fill-missing updates.
    Cover-image URLs replace when upstream differs. Errors are counted, not
    raised — exit code is always 0 so cron schedules don't break.
    """
    import getpass

    from compendium.repositories.sql.creator_repository import SqlCreatorRepository
    from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
    from compendium.services.catalog import CatalogService

    def _on_progress(index, total, work, per) -> None:
        # Verbose: print every line. Quiet: only print actual errors
        # (per is None, or upstream / adapter error — not "no ISBN/UPC"
        # missing-key cases, those are bucketed as skipped and quiet).
        if quiet:
            is_error = per is None or (
                per.error is not None
                and "no ISBN/UPC" not in per.error
                and "no media type" not in per.error
            )
            if not is_error:
                return
        typer.echo(_format_progress_line(index, total, work, per))

    with session_scope() as session:
        catalog = CatalogService(
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            creator_repo=SqlCreatorRepository(session),
            branch_repo=SqlBranchRepository(session),
            media_type_repo=SqlMediaTypeRepository(session),
            audit_svc=AuditService(SqlAuditLogRepository(session)),
            actor_label=f"cli:{getpass.getuser()}",
            source="cli",
        )
        report = catalog.refresh_metadata_bulk(
            media_type_code=media_type,
            branch_code=branch,
            missing_only=missing_only,
            limit=limit,
            dry_run=dry_run,
            on_progress=_on_progress,
        )

    typer.echo("\nRefresh-metadata report:")
    typer.echo(f"  considered  : {report.total_considered}")
    typer.echo(f"  refreshed   : {report.refreshed}")
    typer.echo(f"  no change   : {report.no_change}")
    typer.echo(f"  not found   : {report.not_found}")
    typer.echo(f"  skipped     : {report.skipped_no_key}")
    typer.echo(f"  errored     : {report.errored}")
    if dry_run:
        typer.echo("  (dry-run — no changes persisted)")
    if report.sample_errors:
        typer.echo("\nSample errors:")
        for line in report.sample_errors:
            typer.echo(f"  - {line}")


@app.command("prune-metadata-cache")
def prune_metadata_cache(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be pruned without deleting."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress output when there is nothing to do.",
    ),
) -> None:
    """Delete expired metadata cache rows (past positive or negative TTL)."""
    from compendium.db.session import session_scope
    from compendium.services.metadata_cache import prune_expired

    with session_scope() as session:
        deleted = prune_expired(session, dry_run=dry_run)
    if deleted == 0:
        if quiet:
            return
        typer.echo("Metadata cache: no expired entries found.")
    else:
        verb = "would prune" if dry_run else "pruned"
        typer.echo(f"Metadata cache: {verb} {deleted} expired row(s).")


def _trash_svc(session):
    from compendium.repositories.sql.trash_repository import SqlTrashRepository
    from compendium.services.trash import TrashService

    return TrashService(
        trash_repo=SqlTrashRepository(session),
        work_repo=SqlWorkRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


@app.command("purge-trash")
def purge_trash_cmd(
    older_than_days: int | None = typer.Option(
        None,
        "--older-than-days",
        help="Purge trash entries older than this (default: the trash_retention_days setting).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be purged without deleting."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress output when there is nothing to do.",
    ),
) -> None:
    """Permanently delete trashed works past the retention window."""
    days = older_than_days
    if days is None:
        days = get_site_setting("trash_retention_days")
    if days is not None and days < 0:
        typer.echo(
            "Error: --older-than-days must be at least 1 (or 0 to disable via the setting).",
            err=True,
        )
        raise typer.Exit(1)
    if not days:
        if not quiet:
            typer.echo("Trash retention is disabled (trash_retention_days=0); nothing purged.")
        raise typer.Exit(0)
    with session_scope() as session:
        purged = _trash_svc(session).purge(older_than_days=days, dry_run=dry_run)
    if purged == 0 and quiet:
        return
    verb = "Would purge" if dry_run else "Purged"
    typer.echo(f"{verb} {purged} trash entr{'y' if purged == 1 else 'ies'}.")


@app.command("prune-cover-cache")
def prune_cover_cache(
    max_mb: int = typer.Option(
        500, "--max-mb",
        help="Cache size cap in MB. Oldest files (by mtime) are deleted until under cap.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be pruned without deleting."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress output when there is nothing to do.",
    ),
) -> None:
    """Evict oldest cover-cache files until total size ≤ --max-mb."""
    if max_mb < 1:
        typer.echo("Error: --max-mb must be at least 1.", err=True)
        raise typer.Exit(1)
    from compendium.services.covers import prune

    removed, freed = prune(max_mb * 1024 * 1024, dry_run=dry_run)
    if removed == 0:
        if quiet:
            return
        typer.echo(f"Cover cache under {max_mb} MB cap; nothing to prune.")
    else:
        verb = "Would prune" if dry_run else "Pruned"
        typer.echo(f"{verb} {removed} file(s), freed {freed // 1024} KB.")
