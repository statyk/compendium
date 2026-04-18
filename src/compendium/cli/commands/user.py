import getpass

import typer

from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.audit import AuditService
from compendium.services.auth import AuthService

app = typer.Typer(help="User account commands.")


def _auth_svc(session) -> AuthService:
    return AuthService(
        user_repo=SqlUserRepository(session),
        role_repo=SqlRoleRepository(session),
        settings=get_settings(),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


@app.command("add")
def add_user(
    username: str = typer.Option(..., "--username", prompt=True),
    password: str = typer.Option(
        ..., "--password", prompt=True, hide_input=True, confirmation_prompt=True
    ),
    role: str = typer.Option("Librarian", "--role", help="Role: ReadOnly, Patron, Librarian"),
    email: str | None = typer.Option(None, "--email"),
) -> None:
    """Create a new user account."""
    try:
        with session_scope() as session:
            user = _auth_svc(session).create_user(username, password, role, email=email)
            typer.echo(f"\nCreated user '{user.username}' with role '{role}'.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("deactivate")
def deactivate_user(
    username: str = typer.Option(..., "--username", help="Username to deactivate"),
) -> None:
    """Deactivate a user account (prevents login; does not delete)."""
    try:
        with session_scope() as session:
            user = _auth_svc(session).deactivate_user(username)
            typer.echo(f"\nDeactivated user '{user.username}'.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
