"""CLI for inspecting and editing site_setting overrides.

Reads go through ``get_site_setting`` so an env-var override is reflected
faithfully (and flagged in the listing). Writes go through
``set_site_setting`` with audit logging.
"""

from __future__ import annotations

import getpass
import json
import os

import typer

from compendium.db.session import session_scope
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.services.audit import AuditService
from compendium.services.settings_registry import (
    SettingValidationError,
    UnknownSettingError,
    all_descriptors,
    encode_for_storage,
    get_descriptor,
)
from compendium.services.site_settings import (
    delete_site_setting,
    get_site_setting,
    set_site_setting,
)

app = typer.Typer(help="Inspect and edit site settings (DB-backed configuration).")


def _format_value(v) -> str:
    if v is None:
        return "(unset)"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return json.dumps(v)
    return str(v)


def _audit_svc(session) -> AuditService:
    return AuditService(SqlAuditLogRepository(session))


@app.command("list")
def list_settings(
    scope: str | None = typer.Option(
        None,
        "--scope",
        help="Filter by scope: librarian or system. Default shows both.",
    ),
) -> None:
    """List every registered setting with its current value and source."""
    descs = all_descriptors()
    if scope:
        if scope not in ("librarian", "system"):
            typer.echo("Error: --scope must be 'librarian' or 'system'.", err=True)
            raise typer.Exit(1)
        descs = [d for d in descs if d.scope == scope]

    typer.echo(f"\n{'KEY':<32} {'SCOPE':<10} {'SOURCE':<8} VALUE")
    typer.echo("-" * 80)
    for d in sorted(descs, key=lambda x: (x.scope, x.key)):
        env_set = os.environ.get(d.resolved_env_var()) is not None
        try:
            value = get_site_setting(d.key)
        except SettingValidationError as exc:
            typer.echo(f"{d.key:<32} {d.scope:<10} {'env':<8} ERROR: {exc}")
            continue
        source = "env" if env_set else "db/default"
        typer.echo(f"{d.key:<32} {d.scope:<10} {source:<8} {_format_value(value)}")


@app.command("get")
def get_setting(key: str = typer.Argument(..., help="Setting key to read.")) -> None:
    """Print the current value of a single setting."""
    try:
        get_descriptor(key)
    except UnknownSettingError:
        typer.echo(f"Error: unknown setting {key!r}.", err=True)
        raise typer.Exit(1)
    try:
        value = get_site_setting(key)
    except SettingValidationError as exc:
        typer.echo(f"Error parsing {key}: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(_format_value(value))


@app.command("set")
def set_setting(
    key: str = typer.Argument(..., help="Setting key to write."),
    value: str = typer.Argument(..., help="New value (string; coerced per descriptor type)."),
) -> None:
    """Persist a value to the site_setting table. Env var still wins on read."""
    try:
        desc = get_descriptor(key)
    except UnknownSettingError:
        typer.echo(f"Error: unknown setting {key!r}.", err=True)
        raise typer.Exit(1)
    # Parse the input through the descriptor — same coercion rules as env.
    from compendium.services.settings_registry import parse

    try:
        parsed = parse(desc, value)
    except SettingValidationError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    with session_scope() as session:
        try:
            set_site_setting(
                key,
                parsed,
                session=session,
                audit_svc=_audit_svc(session),
                actor_label=f"cli:{getpass.getuser()}",
                source="cli",
            )
        except SettingValidationError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1)
    if os.environ.get(desc.resolved_env_var()):
        typer.echo(
            f"Warning: env var {desc.resolved_env_var()} is set and will "
            "override this value on read.",
            err=True,
        )
    typer.echo(f"Set {key} = {_format_value(parsed)}.")


@app.command("reset")
def reset_setting(
    key: str = typer.Argument(..., help="Setting key to reset to its default."),
) -> None:
    """Delete the override row so reads fall back to the registered default."""
    try:
        get_descriptor(key)
    except UnknownSettingError:
        typer.echo(f"Error: unknown setting {key!r}.", err=True)
        raise typer.Exit(1)
    with session_scope() as session:
        deleted = delete_site_setting(
            key,
            session=session,
            audit_svc=_audit_svc(session),
            actor_label=f"cli:{getpass.getuser()}",
            source="cli",
        )
    if deleted:
        typer.echo(f"Reset {key} to default.")
    else:
        typer.echo(f"{key} has no override; nothing changed.")
