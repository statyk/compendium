"""compendium secrets — manage encrypted secrets stored in the DB."""
from __future__ import annotations

import getpass

import typer

from compendium.cli.output import Column, emit_list, format_option
from compendium.db.session import session_scope
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.services.audit import AuditService
from compendium.services.secrets import (
    CanaryResult,
    SecretKeyMissingError,
    SecretKeyMismatchError,
    check_canary,
    secret_key_configured,
)
from compendium.services.settings_registry import all_descriptors, get_descriptor
from compendium.services.site_settings import (
    delete_site_setting,
    get_site_setting,
    set_site_setting,
)

app = typer.Typer(help="Manage encrypted secrets stored in the DB (API keys, passwords).")


def _audit_svc(session) -> AuditService:
    return AuditService(SqlAuditLogRepository(session))


def _secret_descriptors():
    return [d for d in all_descriptors() if d.secret]


_COLUMNS = [
    Column("key", "KEY"),
    Column("env_var", "ENV VAR"),
    Column("source", "SOURCE"),
    Column("display_name", "DISPLAY NAME"),
]


@app.command("list")
def list_secrets(format: str = format_option()) -> None:
    """Show registered secrets and their current source (env / db / not set)."""
    if not secret_key_configured():
        typer.echo(
            "Warning: COMPENDIUM_SECRET_KEY is not set. "
            "DB-stored secrets cannot be read or written.",
            err=True,
        )

    descs = _secret_descriptors()
    if not descs:
        emit_list([], _COLUMNS, format, empty="No secrets registered.")
        return

    with session_scope() as session:
        canary = check_canary(session)

    if canary == CanaryResult.MISMATCH:
        typer.echo(
            "Warning: COMPENDIUM_SECRET_KEY does not match the key used to encrypt "
            "stored secrets. DB-stored values are unreadable with the current key.",
            err=True,
        )

    rows = []
    for d in sorted(descs, key=lambda x: x.key):
        env_var = d.resolved_env_var()
        env_set = d.env_overridden()
        if env_set:
            source = "env"
        else:
            from compendium.services.site_settings import _refresh_cache_if_needed, _cache
            _refresh_cache_if_needed()
            db_val = _cache.get(d.key)
            source = "db" if db_val else "not set"
        rows.append(
            {
                "key": d.key,
                "env_var": env_var,
                "source": source,
                "display_name": d.resolved_display_name(),
            }
        )

    emit_list(rows, _COLUMNS, format, empty="No secrets registered.")


def _get_secret_validators() -> dict:
    """Return the pre-save validator registry for secrets."""
    from compendium.web.routes.admin_settings import _SECRET_VALIDATORS
    return _SECRET_VALIDATORS


@app.command("set")
def set_secret(
    key: str = typer.Argument(..., help="Secret key to set (e.g. smtp_password)."),
    value: str | None = typer.Option(
        None, "--value", hide_input=True, help="Value to store (prompted if omitted)."
    ),
    force: bool = typer.Option(
        False, "--force", help="Skip pre-save validation (e.g. GB key live test)."
    ),
) -> None:
    """Encrypt and store a secret in the DB. COMPENDIUM_SECRET_KEY must be set."""
    try:
        desc = get_descriptor(key)
    except Exception:
        typer.echo(f"Error: unknown setting '{key}'.", err=True)
        raise typer.Exit(1)

    if not desc.secret:
        typer.echo(f"Error: '{key}' is not a secret setting. Use 'compendium settings set' instead.", err=True)
        raise typer.Exit(1)

    if value is None:
        value = typer.prompt(f"Value for {key}", hide_input=True, confirmation_prompt=True)

    # Run pre-save validator if registered for this key.
    if not force and value:
        validators = _get_secret_validators()
        validator = validators.get(key)
        if validator is not None:
            typer.echo(f"Validating {key}…")
            result = validator(value)
            if result.warning:
                typer.echo(f"Warning: {result.warning}", err=True)
            if not result.ok:
                typer.echo(f"Validation failed: {result.reason}", err=True)
                save_anyway = typer.confirm("Save anyway?", default=False)
                if not save_anyway:
                    raise typer.Exit(1)
            elif not result.warning:
                typer.echo("  Key validated successfully.")

    try:
        with session_scope() as session:
            set_site_setting(
                key,
                value or None,
                session=session,
                audit_svc=_audit_svc(session),
                actor_label=f"cli:{getpass.getuser()}",
                source="cli",
            )
    except (SecretKeyMissingError, SecretKeyMismatchError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    if desc.env_overridden():
        typer.echo(
            f"Warning: {desc.resolved_env_var()} is also set in the environment and will "
            "take precedence over the DB value on read.",
            err=True,
        )
    typer.echo(f"Stored {key} (encrypted).")


@app.command("clear")
def clear_secret(
    key: str = typer.Argument(..., help="Secret key to clear."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Remove a stored secret from the DB (reverts to env-only or unset)."""
    try:
        desc = get_descriptor(key)
    except Exception:
        typer.echo(f"Error: unknown setting '{key}'.", err=True)
        raise typer.Exit(1)

    if not desc.secret:
        typer.echo(f"Error: '{key}' is not a secret setting. Use 'compendium settings reset' instead.", err=True)
        raise typer.Exit(1)

    if not yes:
        typer.confirm(f"Clear stored secret '{key}'?", abort=True)

    with session_scope() as session:
        deleted = delete_site_setting(
            key,
            session=session,
            audit_svc=_audit_svc(session),
            actor_label=f"cli:{getpass.getuser()}",
            source="cli",
        )

    if deleted:
        typer.echo(f"Cleared {key}.")
    else:
        typer.echo(f"{key} had no stored value; nothing changed.")
