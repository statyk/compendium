"""CLI commands for backing up and restoring Compendium."""
from __future__ import annotations

from pathlib import Path

import typer

from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.services.backup import BackupError, BackupService


def backup_command(
    output: Path = typer.Option(..., "--output", "-o", help="Path to write the backup tarball."),
    no_covers: bool = typer.Option(False, "--no-covers", help="Skip the cover image cache."),
    no_audit: bool = typer.Option(False, "--no-audit", help="Exclude the audit log from the backup."),
) -> None:
    """Write a portable backup tarball."""
    settings = get_settings()
    with session_scope() as session:
        svc = BackupService(session, settings)
        try:
            manifest = svc.create(
                output, include_covers=not no_covers, include_audit=not no_audit
            )
        except BackupError as exc:
            typer.secho(f"Backup failed: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

    total = sum(manifest["tables"].values())
    typer.echo(f"Backup written to {output}")
    typer.echo(f"  revision:   {manifest['alembic_head']}")
    typer.echo(f"  source:     {manifest['source_backend']}")
    typer.echo(f"  rows total: {total}")


def restore_command(
    archive: Path = typer.Argument(..., help="Path to a backup tarball."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing database."),
    no_covers: bool = typer.Option(False, "--no-covers", help="Skip restoring the cover image cache."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the overwrite confirmation prompt."),
) -> None:
    """Restore from a backup tarball.

    Automatically migrates the target schema to the backup's Alembic
    revision, inserts rows, then replays migrations forward to head.
    """
    settings = get_settings()
    if force and not yes:
        typer.confirm(
            f"This will wipe the database at {settings.database_url} and replace it "
            f"with {archive}. Continue?",
            abort=True,
        )

    with session_scope() as session:
        svc = BackupService(session, settings)
        try:
            manifest = svc.restore(archive, force=force, include_covers=not no_covers)
        except BackupError as exc:
            typer.secho(f"Restore failed: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

    total = sum(manifest["tables"].values())
    typer.echo(f"Restored {total} rows from {archive}")
    typer.echo(f"  backup revision: {manifest['alembic_head']}")
    typer.echo(f"  source backend:  {manifest.get('source_backend', 'unknown')}")
