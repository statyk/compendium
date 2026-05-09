"""CLI commands for inspecting and managing the metadata cache."""

from __future__ import annotations

import getpass

import typer

app = typer.Typer(help="Metadata utilities.")
cache_app = typer.Typer(help="Metadata cache management.")
app.add_typer(cache_app, name="cache")
gb_quota_app = typer.Typer(help="Google Books daily-quota circuit breaker.")
app.add_typer(gb_quota_app, name="gb-quota")


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


@gb_quota_app.command("status")
def gb_quota_status() -> None:
    """Show whether the Google Books daily quota is currently exhausted."""
    from compendium.services.metadata import is_gb_quota_exhausted, _GB_QUOTA_ADAPTER, _GB_QUOTA_KIND, _GB_QUOTA_VALUE
    from compendium.db.session import session_scope
    from compendium.domain.models import MetadataCache

    with session_scope() as s:
        entry = s.get(MetadataCache, (_GB_QUOTA_ADAPTER, _GB_QUOTA_KIND, _GB_QUOTA_VALUE))

    if entry is None:
        typer.echo("Google Books quota: not exhausted")
        return

    if is_gb_quota_exhausted():
        typer.echo(f"Google Books quota: exhausted (hit at {entry.fetched_at.isoformat()})")
        typer.echo("  Book lookups are falling back to Open Library until the quota resets (~24 h).")
        typer.echo("  Use 'compendium metadata gb-quota clear' to force an early reset.")
    else:
        typer.echo(f"Google Books quota: sentinel exists but has expired (hit at {entry.fetched_at.isoformat()})")
        typer.echo("  Quota has auto-reset; Google Books will be used again on the next lookup.")


@gb_quota_app.command("clear")
def gb_quota_clear(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Remove the quota-exhausted sentinel so Google Books resumes immediately."""
    if not yes:
        confirm = typer.confirm(
            "Clear the Google Books quota-exhausted flag? "
            "Google Books will be tried again on the next book lookup.",
            default=False,
        )
        if not confirm:
            typer.echo("Aborted.")
            raise typer.Exit(0)

    from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
    from compendium.services.audit import AuditAction, AuditEntityType, AuditService
    from compendium.services.metadata import clear_gb_quota_exhausted

    actor_label = f"cli:{getpass.getuser()}"
    existed = clear_gb_quota_exhausted()
    if not existed:
        typer.echo("No quota-exhausted sentinel found — nothing to clear.")
        return

    from compendium.db.session import session_scope

    with session_scope() as s:
        audit_svc = AuditService(SqlAuditLogRepository(s))
        audit_svc.record(
            actor=None,
            actor_label=actor_label,
            source="cli",
            entity_type=AuditEntityType.METADATA_CACHE,
            entity_id=None,
            action=AuditAction.SETTING_RESET,
            details={"cleared": "google_books_quota_sentinel"},
        )
    typer.echo("Google Books quota sentinel cleared. Book lookups will use Google Books again.")
