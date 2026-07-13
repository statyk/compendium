from __future__ import annotations

import getpass
import os

import typer

from compendium.cli.output import Column, emit_list, format_option
from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.audit import AuditService
from compendium.services.auth import AuthService, assignable_roles
from compendium.services.patrons import PatronService

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


def _patron_svc(session) -> PatronService:
    return PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


def _administrator_exists(session) -> bool:
    from compendium.domain.models import AppUser, Role
    return (
        session.query(AppUser)
        .join(Role, AppUser.role_id == Role.id)
        .filter(Role.name == "Administrator", AppUser.is_active == True)  # noqa: E712
        .first()
    ) is not None


def _log_bootstrap_override(session, username: str, role: str) -> None:
    AuditService(SqlAuditLogRepository(session)).record(
        actor=None,
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
        entity_type="USER",
        entity_id=None,
        action="CREATE",
        details={"bootstrap_override": True, "username": username, "role": role},
    )


@app.command("add")
def add_user(
    username: str = typer.Option(..., "--username", prompt=True),
    password: str = typer.Option(
        ..., "--password", prompt=True, hide_input=True, confirmation_prompt=True
    ),
    role: str = typer.Option(
        "Administrator",
        "--role",
        help="Role: ReadOnly, Patron, Librarian, SystemAdmin, Administrator",
    ),
    email: str | None = typer.Option(None, "--email"),
    create_patron: bool = typer.Option(False, "--create-patron", help="Also create a patron record (requires --role Patron)"),
    patron_name: str | None = typer.Option(None, "--patron-name", help="Full name for the new patron record"),
    link_patron: str | None = typer.Option(None, "--link-patron", help="Library card number of an existing patron to link (requires --role Patron)"),
    allow_bootstrap: bool = typer.Option(
        False,
        "--allow-bootstrap",
        help="Bypass the actor check when re-bootstrapping an existing database (audit-logged).",
    ),
) -> None:
    """Create a new user account."""
    if create_patron and link_patron:
        typer.echo("Error: --create-patron and --link-patron are mutually exclusive", err=True)
        raise typer.Exit(1)
    if (create_patron or link_patron) and role != "Patron":
        typer.echo("Error: --create-patron and --link-patron require --role Patron", err=True)
        raise typer.Exit(1)
    try:
        with session_scope() as session:
            actor_username = os.environ.get("COMPENDIUM_ACTOR_USERNAME")
            if actor_username:
                actor = SqlUserRepository(session).get_by_username(actor_username)
                if actor is None:
                    typer.echo(f"Error: COMPENDIUM_ACTOR_USERNAME '{actor_username}' not found", err=True)
                    raise typer.Exit(1)
                all_roles = SqlRoleRepository(session).list()
                allowed_names = {r.name for r in assignable_roles(actor.role.permissions, all_roles)}
                if role not in allowed_names:
                    typer.echo(f"Error: Your account cannot assign the '{role}' role.", err=True)
                    raise typer.Exit(1)
            elif _administrator_exists(session):
                if not allow_bootstrap:
                    typer.echo(
                        "Error: An Administrator already exists. Set COMPENDIUM_ACTOR_USERNAME "
                        "to an existing user whose permissions cover the requested role, or pass "
                        "--allow-bootstrap if you genuinely need to re-bootstrap "
                        "(this will be audit-logged).",
                        err=True,
                    )
                    raise typer.Exit(1)
                _log_bootstrap_override(session, username, role)

            new_user = _auth_svc(session).create_user(username, password, role, email=email)

            if create_patron:
                name = patron_name or typer.prompt("Patron full name")
                _patron_svc(session).create(
                    full_name=name,
                    user_id=new_user.id,
                )
            elif link_patron:
                _patron_svc(session).link_user(link_patron, new_user.id)

            typer.echo(f"\nCreated user '{new_user.username}' with role '{role}'.")
            if create_patron:
                typer.echo(f"  Patron record created and linked.")
            elif link_patron:
                typer.echo(f"  Linked to patron card: {link_patron}")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("set-role")
def set_user_role(
    username: str = typer.Option(..., "--username", help="Username to update"),
    role: str = typer.Option(..., "--role", help="New role name"),
) -> None:
    """Change the role assigned to a user account."""
    try:
        with session_scope() as session:
            actor_username = os.environ.get("COMPENDIUM_ACTOR_USERNAME")
            if not actor_username:
                typer.echo(
                    "Error: Set COMPENDIUM_ACTOR_USERNAME to an existing user whose "
                    "permissions cover the requested role assignment.",
                    err=True,
                )
                raise typer.Exit(1)
            actor = SqlUserRepository(session).get_by_username(actor_username)
            if actor is None:
                typer.echo(f"Error: COMPENDIUM_ACTOR_USERNAME '{actor_username}' not found", err=True)
                raise typer.Exit(1)
            all_roles = SqlRoleRepository(session).list()
            allowed_names = {r.name for r in assignable_roles(actor.role.permissions, all_roles)}
            if role not in allowed_names:
                typer.echo(f"Error: Your account cannot assign the '{role}' role.", err=True)
                raise typer.Exit(1)
            user = _auth_svc(session).update_role(username, role)
            typer.echo(f"\nUser '{user.username}' role set to '{user.role.name}'.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("set-password")
def set_user_password(
    username: str = typer.Option(..., "--username", help="Username to update"),
    password: str | None = typer.Option(
        None,
        "--password",
        help="New password. Prompted if omitted.",
        hide_input=True,
    ),
) -> None:
    """Reset a user's password."""
    if password is None:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        with session_scope() as session:
            user = _auth_svc(session).set_password(username, password)
            typer.echo(f"\nPassword reset for user '{user.username}'.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("list")
def list_users(
    limit: int = typer.Option(50, "--limit"),
    include_inactive: bool = typer.Option(False, "--include-inactive", help="Include inactive accounts"),
    format: str = format_option(),
) -> None:
    """List user accounts (active only by default)."""
    with session_scope() as session:
        users = SqlUserRepository(session).list(limit=limit, include_inactive=include_inactive)
        emit_list(
            [{
                "username": u.username,
                "role": u.role.name,
                "is_active": u.is_active,
            } for u in users],
            [Column("username", "Username"),
             Column("role", "Role"),
             Column("is_active", "Active", formatter=lambda v: "" if v else "inactive")],
            format,
            empty="No users found.",
        )


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


@app.command("reactivate")
def reactivate_user(
    username: str = typer.Option(..., "--username", help="Username to reactivate"),
) -> None:
    """Reactivate an inactive user account."""
    try:
        with session_scope() as session:
            user = _auth_svc(session).reactivate_user(username)
            typer.echo(f"\nReactivated user '{user.username}'.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("link-patron")
def link_patron_cmd(
    username: str = typer.Option(..., "--username", help="Username to update"),
    card: str = typer.Option(..., "--card", help="Patron library card number to link"),
) -> None:
    """Link an existing card-only patron to a user account."""
    try:
        with session_scope() as session:
            target = SqlUserRepository(session).get_by_username(username)
            if target is None:
                typer.echo(f"Error: No user with username '{username}'", err=True)
                raise typer.Exit(1)
            patron = _patron_svc(session).link_user(card, target.id)
            typer.echo(f"\nLinked patron {patron.full_name} ({card}) to user '{username}'.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("unlink-patron")
def unlink_patron_cmd(
    username: str = typer.Option(..., "--username", help="Username to update"),
) -> None:
    """Remove the linked patron record from a user account."""
    try:
        with session_scope() as session:
            target = SqlUserRepository(session).get_by_username(username)
            if target is None:
                typer.echo(f"Error: No user with username '{username}'", err=True)
                raise typer.Exit(1)
            from compendium.repositories.sql.patron_repository import SqlPatronRepository as _PR
            patron = _PR(session).get_by_user_id(target.id)
            if patron is None:
                typer.echo(f"Error: User '{username}' has no linked patron record.", err=True)
                raise typer.Exit(1)
            _patron_svc(session).unlink_user(patron.library_card_number)
            typer.echo(f"\nUnlinked patron from user '{username}'.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
