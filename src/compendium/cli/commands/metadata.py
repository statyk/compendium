"""CLI commands for inspecting and managing the metadata cache."""

from __future__ import annotations

import getpass

import typer

app = typer.Typer(help="Metadata utilities.")
cache_app = typer.Typer(help="Metadata cache management.")
app.add_typer(cache_app, name="cache")


@cache_app.command("stats")
def cache_stats() -> None:
    """Print metadata cache row counts and oldest entry."""
    from compendium.db.session import session_scope
    from compendium.services.metadata_cache import get_stats

    with session_scope() as session:
        stats = get_stats(session)

    typer.echo(f"Total rows:       {stats.total}")
    typer.echo(f"  Positive (hit): {stats.positive}")
    typer.echo(f"  Negative (miss): {stats.negative}")
    typer.echo(f"  Expired positive: {stats.expired_positive}")
    typer.echo(f"  Expired negative: {stats.expired_negative}")
    if stats.oldest_fetched_at:
        typer.echo(f"Oldest entry:     {stats.oldest_fetched_at.isoformat()}")
    else:
        typer.echo("Oldest entry:     (none)")
    if stats.adapter_counts:
        typer.echo("By adapter:")
        for adapter, count in stats.adapter_counts.items():
            typer.echo(f"  {adapter}: {count}")


@cache_app.command("clear")
def cache_clear(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Delete all metadata cache rows (audited)."""
    if not yes:
        confirm = typer.confirm("Delete all metadata cache rows?", default=False)
        if not confirm:
            typer.echo("Aborted.")
            raise typer.Exit(0)

    from compendium.db.session import session_scope
    from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
    from compendium.services.audit import AuditAction, AuditEntityType, AuditService
    from compendium.services.metadata_cache import clear_all

    actor_label = f"cli:{getpass.getuser()}"
    with session_scope() as session:
        deleted = clear_all(session)
        audit_svc = AuditService(SqlAuditLogRepository(session))
        audit_svc.record(
            actor=None,
            actor_label=actor_label,
            source="cli",
            entity_type=AuditEntityType.METADATA_CACHE,
            entity_id=None,
            action=AuditAction.SETTING_RESET,
            details={"deleted_rows": deleted},
        )
    typer.echo(f"Cleared {deleted} metadata cache row(s).")
