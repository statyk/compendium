"""CLI commands for backing up and restoring Compendium."""
from __future__ import annotations

import typer

from compendium.cli.io import is_stdio, open_input, open_output
from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.services.backup import BackupError, BackupService


def backup_command(
    output: str = typer.Option(
        ...,
        "--output",
        "-o",
        help="Path to write the backup tarball. Use '-' for stdout.",
    ),
    no_covers: bool = typer.Option(False, "--no-covers", help="Skip the cover image cache."),
    no_audit: bool = typer.Option(False, "--no-audit", help="Exclude the audit log from the backup."),
) -> None:
    """Write a portable backup tarball."""
    settings = get_settings()
    to_stdout = is_stdio(output)
    with session_scope() as session:
        svc = BackupService(session, settings)
        try:
            with open_output(output, binary=True) as fout:
                if to_stdout:
                    manifest = svc.create(
                        output_fileobj=fout,
                        include_covers=not no_covers,
                        include_audit=not no_audit,
                    )
                else:
                    manifest = svc.create(
                        output,
                        include_covers=not no_covers,
                        include_audit=not no_audit,
                    )
        except BackupError as exc:
            typer.secho(f"Backup failed: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

    total = sum(manifest["tables"].values())
    # When writing to stdout, status messages must not corrupt the binary stream.
    err = to_stdout
    where = "stdout" if to_stdout else output
    typer.echo(f"Backup written to {where}", err=err)
    typer.echo(f"  revision:   {manifest['alembic_head']}", err=err)
    typer.echo(f"  source:     {manifest['source_backend']}", err=err)
    typer.echo(f"  rows total: {total}", err=err)


def restore_command(
    archive: str = typer.Argument(
        ..., help="Path to a backup tarball. Use '-' for stdin."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing database."),
    no_covers: bool = typer.Option(False, "--no-covers", help="Skip restoring the cover image cache."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the overwrite confirmation prompt."),
) -> None:
    """Restore from a backup tarball.

    Automatically migrates the target schema to the backup's Alembic
    revision, inserts rows, then replays migrations forward to head.
    """
    settings = get_settings()
    from_stdin = is_stdio(archive)
    if force and not yes:
        source_label = "stdin" if from_stdin else archive
        typer.confirm(
            f"This will wipe the database at {settings.database_url} and replace it "
            f"with {source_label}. Continue?",
            abort=True,
        )

    with session_scope() as session:
        svc = BackupService(session, settings)
        try:
            with open_input(archive, binary=True) as fin:
                if from_stdin:
                    manifest = svc.restore(
                        input_fileobj=fin,
                        force=force,
                        include_covers=not no_covers,
                    )
                else:
                    manifest = svc.restore(
                        archive, force=force, include_covers=not no_covers
                    )
        except BackupError as exc:
            typer.secho(f"Restore failed: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)

    total = sum(manifest["tables"].values())
    where = "stdin" if from_stdin else archive
    typer.echo(f"Restored {total} rows from {where}")
    typer.echo(f"  backup revision: {manifest['alembic_head']}")
    typer.echo(f"  source backend:  {manifest.get('source_backend', 'unknown')}")
