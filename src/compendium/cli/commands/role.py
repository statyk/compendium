import getpass

import typer

from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.services.audit import AuditService
from compendium.services.roles import RoleService

app = typer.Typer(help="Role management commands.")


def _role_svc(session) -> RoleService:
    return RoleService(
        role_repo=SqlRoleRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


@app.command("list")
def list_roles() -> None:
    """List all roles."""
    with session_scope() as session:
        roles = _role_svc(session).list()
        if not roles:
            typer.echo("No roles found.")
            return
        typer.echo("\nRoles:")
        for r in roles:
            system_flag = "  [PRESET]" if r.is_system else ""
            perm_summary = "*" if "*" in r.permissions else ", ".join(r.permissions) or "(none)"
            typer.echo(f"  #{r.id}  {r.name}{system_flag}")
            typer.echo(f"       permissions: {perm_summary}")


@app.command("create")
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


@app.command("update")
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
