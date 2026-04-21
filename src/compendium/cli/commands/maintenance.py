from datetime import datetime, timedelta, timezone

import typer

from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.holds import HoldService

app = typer.Typer(help="Maintenance commands (intended for cron/systemd).")


@app.command("expire-holds")
def expire_holds() -> None:
    """Expire waiting holds whose expiry date has passed."""
    with session_scope() as session:
        svc = HoldService(
            hold_repo=SqlHoldRepository(session),
            patron_repo=SqlPatronRepository(session),
            work_repo=SqlWorkRepository(session),
            branch_repo=SqlBranchRepository(session),
            hold_expiry_days=get_settings().hold_expiry_days,
        )
        count = svc.expire_holds()
        typer.echo(f"Expired {count} hold(s).")


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
