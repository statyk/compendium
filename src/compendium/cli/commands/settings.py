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
    env_only_field_names,
    get_descriptor,
)
from compendium.services.site_settings import (
    delete_site_setting,
    get_site_setting,
    set_site_setting,
)

app = typer.Typer(help="Inspect and edit site settings (DB-backed configuration).")

# Settings whose value should be masked in `settings list` output unless the
# operator passes --show-secrets. Includes anything that could leak a secret
# (DB URL embeds the password, JWT key signs tokens, etc.) plus all
# descriptors marked secret=True in the registry.
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {"database_url", "jwt_secret_key"}
    | {d.key for d in all_descriptors() if d.secret}
)


def _format_value(v) -> str:
    if v is None:
        return "(unset)"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return json.dumps(v)
    return str(v)


def _format_listing_value(key: str, value, *, show_secrets: bool) -> str:
    if not show_secrets and key in _SENSITIVE_KEYS and value not in (None, ""):
        return "********"
    return _format_value(value)


def _audit_svc(session) -> AuditService:
    return AuditService(SqlAuditLogRepository(session))


@app.command("list")
def list_settings(
    scope: str | None = typer.Option(
        None,
        "--scope",
        help="Filter by scope: librarian, system, or env-only. Default shows all.",
    ),
    all_settings: bool = typer.Option(
        False,
        "--all",
        help="Include env-only Settings fields (DB URL, JWT secret, etc.).",
    ),
    show_secrets: bool = typer.Option(
        False,
        "--show-secrets",
        help="Print sensitive values in plaintext (default: masked as ********).",
    ),
) -> None:
    """List recognized settings with their current value, source, and env var.

    By default lists DB-editable settings only. Pass ``--all`` to also include
    env-only settings (those defined on the Pydantic ``Settings`` model that
    aren't migrated to the DB-editable registry).

    Source values:
      env     — env var is set; that's what readers will see
      db      — env not set; a row exists in site_setting
      default — env not set; no row; falls back to the registered default
    """
    valid_scopes = {"librarian", "system", "env-only"}
    if scope and scope not in valid_scopes:
        typer.echo(
            f"Error: --scope must be one of {sorted(valid_scopes)}.", err=True
        )
        raise typer.Exit(1)

    rows: list[tuple[str, str, str, str, str]] = []  # key, env_var, scope, source, formatted_value

    # Registry items (DB-editable).
    if scope != "env-only":
        descs = all_descriptors()
        if scope:
            descs = [d for d in descs if d.scope == scope]
        for d in sorted(descs, key=lambda x: (x.scope, x.key)):
            env_var = d.resolved_env_var()
            env_set = d.env_overridden()
            try:
                value = get_site_setting(d.key)
            except SettingValidationError as exc:
                rows.append((d.key, env_var, d.scope, "env", f"ERROR: {exc}"))
                continue
            source = _resolve_source_for_registry(d.key, env_set)
            formatted = _format_listing_value(d.key, value, show_secrets=show_secrets)
            rows.append((d.key, env_var, d.scope, source, formatted))

    # Env-only items (Pydantic Settings fields outside the registry).
    if all_settings or scope == "env-only":
        from compendium.config.settings import Settings

        s = Settings()
        for name in env_only_field_names():
            env_var = f"COMPENDIUM_{name.upper()}"
            env_set = bool(os.environ.get(env_var))
            value = getattr(s, name)
            source = "env" if env_set else "default"
            formatted = _format_listing_value(name, value, show_secrets=show_secrets)
            rows.append((name, env_var, "env-only", source, formatted))

    rows.sort(key=lambda r: (r[2], r[0]))

    header = f"{'KEY':<32} {'ENV VAR':<42} {'SCOPE':<10} {'SOURCE':<8} VALUE"
    typer.echo("\n" + header)
    typer.echo("-" * len(header))
    for key, env_var, scope_val, source, formatted in rows:
        typer.echo(f"{key:<32} {env_var:<42} {scope_val:<10} {source:<8} {formatted}")


def _resolve_source_for_registry(key: str, env_set: bool) -> str:
    """Distinguish env / db / default for a registered descriptor.

    Cheap path: if the env var is set, env wins on read regardless of any DB
    row, so report 'env'. Otherwise we have to peek at the site_setting table
    to tell 'db' (override row exists) from 'default' (no row).

    Uses ``engine_mod.get_engine()`` rather than ``session_scope`` so test
    fixtures that patch ``compendium.db.engine.get_engine`` are honored
    (function-local module access avoids the from-X-import-Y patch trap).
    """
    if env_set:
        return "env"
    from sqlalchemy.orm import Session

    from compendium.db import engine as engine_mod
    from compendium.repositories.sql.site_setting_repository import (
        SqlSiteSettingRepository,
    )

    with Session(engine_mod.get_engine()) as session:
        row = SqlSiteSettingRepository(session).get(key)
    return "db" if row is not None else "default"


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
    if desc.env_overridden():
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
