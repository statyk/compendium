import getpass
import os

import typer

from compendium.cli.output import Column, emit_list, format_option
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.audit import AuditService
from compendium.services.roles import RoleService

app = typer.Typer(help="Role management commands.")


def _role_svc(session) -> RoleService:
    actor = None
    actor_username = os.environ.get("COMPENDIUM_ACTOR_USERNAME")
    if actor_username:
        actor = SqlUserRepository(session).get_by_username(actor_username)
        if actor is None:
            typer.echo(f"Error: COMPENDIUM_ACTOR_USERNAME '{actor_username}' not found", err=True)
            raise typer.Exit(1)
    elif SqlUserRepository(session).list(limit=1):
        typer.echo(
            "Error: Users exist in this database. Set COMPENDIUM_ACTOR_USERNAME to "
            "an active user whose permissions cover the role operations you want to perform.",
            err=True,
        )
        raise typer.Exit(1)
    return RoleService(
        role_repo=SqlRoleRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


@app.command("list")
def list_roles(format: str = format_option()) -> None:
    """List all roles."""
    with session_scope() as session:
        roles = _role_svc(session).list()
        emit_list(
            [{
                "id": r.id,
                "name": r.name,
                "is_system": r.is_system,
                "permissions": r.permissions,
            } for r in roles],
            [Column("id", "#", justify="right"),
             Column("name", "Name"),
             Column("is_system", "Preset", formatter=lambda v: "PRESET" if v else ""),
             Column("permissions", "Permissions",
                    formatter=lambda v: "*" if "*" in v else (", ".join(v) or "(none)"))],
            format,
            empty="No roles found.",
        )


@app.command("add")
def create_role(
    name: str = typer.Option(..., "--name", help="Role name"),
    permissions: str = typer.Option("", "--permissions", help="Comma-separated permission strings"),
    full_access: bool = typer.Option(False, "--full-access", help="Grant full access (stores [\"*\"])"),
) -> None:
    """Create a new custom role."""
    perms: list[str] = ["*"] if full_access else [p.strip() for p in permissions.split(",") if p.strip()]
    try:
        with session_scope() as session:
            role = _role_svc(session).create(name=name, permissions=perms)
            typer.echo(f"\nCreated role #{role.id} '{role.name}'.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("edit")
def update_role(
    role_id: int = typer.Option(..., "--id", help="Role ID to update"),
    name: str = typer.Option(None, "--name", help="New role name"),
    permissions: str = typer.Option(None, "--permissions", help="Comma-separated permission strings (replaces all)"),
    full_access: bool = typer.Option(None, "--full-access/--no-full-access", help="Set full access ([\"*\"])"),
) -> None:
    """Update a custom role's name or permissions. Preset roles cannot be edited."""
    new_perms: list[str] | None = None
    if full_access is True:
        new_perms = ["*"]
    elif full_access is False:
        new_perms = []
    elif permissions is not None:
        new_perms = [p.strip() for p in permissions.split(",") if p.strip()]

    if name is None and new_perms is None:
        typer.echo("Specify at least one of --name, --permissions, or --full-access/--no-full-access.", err=True)
        raise typer.Exit(1)
    try:
        with session_scope() as session:
            role = _role_svc(session).update(role_id, name=name, permissions=new_perms)
            typer.echo(f"\nUpdated role #{role.id} '{role.name}'.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("clone")
def clone_role(
    role_id: int = typer.Option(..., "--id", help="Role ID to clone"),
    name: str = typer.Option(..., "--name", help="Name for the new role"),
) -> None:
    """Clone a role (including preset roles) into a new editable custom role."""
    try:
        with session_scope() as session:
            role = _role_svc(session).clone(role_id, new_name=name)
            typer.echo(f"\nCloned to new role #{role.id} '{role.name}'.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


from compendium.cli.io import register_alias  # noqa: E402

register_alias(app, "create", create_role)
register_alias(app, "update", update_role)
