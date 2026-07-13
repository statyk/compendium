import getpass
from datetime import date, datetime

import typer

from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.db.engine import get_settings
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_category_repository import (
    SqlPatronCategoryRepository,
)
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.audit import AuditService
from compendium.services.auth import AuthService
from compendium.services.patrons import PatronService

app = typer.Typer(help="Patron management commands.")


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise typer.BadParameter(f"--expires must be YYYY-MM-DD, got '{s}'") from exc


def _resolve_category_id(session, code: str | None) -> int | None:
    if code is None:
        return None
    cat = SqlPatronCategoryRepository(session).get_by_code(code.lower())
    if cat is None:
        raise typer.BadParameter(f"No patron category with code '{code}'")
    return cat.id


def _patron_svc(session) -> PatronService:
    return PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


def _patron_svc_with_auth(session) -> PatronService:
    auth_svc = AuthService(
        user_repo=SqlUserRepository(session),
        role_repo=SqlRoleRepository(session),
        settings=get_settings(),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )
    return PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
        auth_svc=auth_svc,
    )


@app.command("add")
def add_patron(
    name: str = typer.Option(..., "--name", help="Patron's full name"),
    email: str | None = typer.Option(None, "--email"),
    phone: str | None = typer.Option(None, "--phone"),
    link_user: str | None = typer.Option(None, "--link-user", help="Username to link to this patron"),
    create_user: bool = typer.Option(False, "--create-user", help="Create a new Patron-role login for this patron"),
    username: str | None = typer.Option(None, "--username", help="Username for the new login (requires --create-user)"),
    password: str | None = typer.Option(None, "--password", help="Password for the new login (prompted if omitted)", hide_input=True),
    category: str | None = typer.Option(None, "--category", help="Patron category code (adult/child/staff/teacher)"),
    expires: str | None = typer.Option(None, "--expires", help="Card expiry date (YYYY-MM-DD)"),
) -> None:
    """Register a new patron."""
    if link_user and create_user:
        typer.echo("Error: --link-user and --create-user are mutually exclusive", err=True)
        raise typer.Exit(1)
    try:
        with session_scope() as session:
            category_id = _resolve_category_id(session, category)
            expires_at = _parse_date(expires) if expires else None
            if create_user:
                if not username:
                    username = typer.prompt("Username")
                if not password:
                    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
                patron = _patron_svc_with_auth(session).create_with_account(
                    full_name=name,
                    contact_email=email,
                    contact_phone=phone,
                    category_id=category_id,
                    expires_at=expires_at,
                    username=username,
                    password=password,
                )
            else:
                user_id: int | None = None
                if link_user:
                    u = SqlUserRepository(session).get_by_username(link_user)
                    if u is None:
                        typer.echo(f"Error: No user with username '{link_user}'", err=True)
                        raise typer.Exit(1)
                    user_id = u.id
                patron = _patron_svc(session).create(
                    full_name=name,
                    contact_email=email,
                    contact_phone=phone,
                    user_id=user_id,
                    category_id=category_id,
                    expires_at=expires_at,
                )
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"\nPatron registered: {patron.full_name}")
    typer.echo(f"  Card number : {patron.library_card_number}")
    if email:
        typer.echo(f"  Email       : {email}")
    if phone:
        typer.echo(f"  Phone       : {phone}")
    if create_user:
        typer.echo(f"  Login       : {username} (Patron role)")
    elif link_user:
        typer.echo(f"  Linked user : {link_user}")
    if category:
        typer.echo(f"  Category    : {category}")
    if expires:
        typer.echo(f"  Expires     : {expires}")


@app.command("set")
def set_patron(
    card: str = typer.Option(..., "--card", help="Patron library card number"),
    category: str | None = typer.Option(None, "--category", help="Patron category code"),
    clear_category: bool = typer.Option(False, "--clear-category", help="Remove the category"),
    expires: str | None = typer.Option(None, "--expires", help="Card expiry date (YYYY-MM-DD)"),
    clear_expires: bool = typer.Option(False, "--clear-expires", help="Remove the expiry date"),
) -> None:
    """Edit category and/or expiry on an existing patron."""
    if category and clear_category:
        typer.echo("Error: --category and --clear-category are mutually exclusive", err=True)
        raise typer.Exit(1)
    if expires and clear_expires:
        typer.echo("Error: --expires and --clear-expires are mutually exclusive", err=True)
        raise typer.Exit(1)
    if not any([category, clear_category, expires, clear_expires]):
        typer.echo("Error: nothing to update (pass --category/--expires/--clear-*)", err=True)
        raise typer.Exit(1)

    from compendium.services.patrons import _MISSING

    try:
        with session_scope() as session:
            cat_arg: object = _MISSING
            if category:
                cat_arg = _resolve_category_id(session, category)
            elif clear_category:
                cat_arg = None
            exp_arg: object = _MISSING
            if expires:
                exp_arg = _parse_date(expires)
            elif clear_expires:
                exp_arg = None
            patron = _patron_svc(session).update(
                card, category_id=cat_arg, expires_at=exp_arg
            )
            typer.echo(f"\nUpdated patron {patron.full_name} ({patron.library_card_number})")
            if patron.category_id is not None:
                typer.echo(f"  Category    : {patron.category.code if patron.category else patron.category_id}")
            if patron.expires_at is not None:
                typer.echo(f"  Expires     : {patron.expires_at.isoformat()}")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("deactivate")
def deactivate_patron(
    card: str = typer.Option(..., "--card", help="Patron library card number"),
) -> None:
    """Deactivate a patron account (cancels active holds; blocks if active loans exist)."""
    try:
        with session_scope() as session:
            patron = _patron_svc(session).deactivate(card)
            typer.echo(f"\nDeactivated: {patron.full_name} ({patron.library_card_number})")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("reactivate")
def reactivate_patron(
    card: str = typer.Option(..., "--card", help="Patron library card number"),
) -> None:
    """Reactivate an inactive patron account."""
    try:
        with session_scope() as session:
            patron = _patron_svc(session).reactivate(card)
            typer.echo(f"\nReactivated: {patron.full_name} ({patron.library_card_number})")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("link-user")
def link_user_cmd(
    card: str = typer.Option(..., "--card", help="Patron library card number"),
    username: str = typer.Option(..., "--username", help="Username to link"),
) -> None:
    """Link a user account to a patron record."""
    try:
        with session_scope() as session:
            u = SqlUserRepository(session).get_by_username(username)
            if u is None:
                typer.echo(f"Error: No user with username '{username}'", err=True)
                raise typer.Exit(1)
            patron = _patron_svc(session).link_user(card, u.id)
            typer.echo(f"\nLinked user '{username}' to patron {patron.full_name} ({card})")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("unlink-user")
def unlink_user_cmd(
    card: str = typer.Option(..., "--card", help="Patron library card number"),
) -> None:
    """Remove the linked user account from a patron record."""
    try:
        with session_scope() as session:
            patron = _patron_svc(session).unlink_user(card)
            typer.echo(f"\nUnlinked user from patron {patron.full_name} ({card})")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("create-user")
def create_user_for_patron(
    card: str = typer.Option(..., "--card", help="Patron library card number"),
    username: str = typer.Option(..., "--username", help="Username for the new login"),
    password: str | None = typer.Option(
        None, "--password", hide_input=True, help="Password (prompted if omitted)"
    ),
) -> None:
    """Create a Patron-role login and link it to an existing card-only patron."""
    if not password:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        with session_scope() as session:
            patron = _patron_svc_with_auth(session).create_account_for_patron(
                card, username=username, password=password
            )
            typer.echo(f"\nCreated login '{username}' and linked to patron {patron.full_name} ({card})")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("list")
def list_patrons(
    limit: int = typer.Option(20, "--limit"),
    include_inactive: bool = typer.Option(False, "--include-inactive", help="Include inactive patrons"),
) -> None:
    """List registered patrons (active only by default)."""
    with session_scope() as session:
        patrons = SqlPatronRepository(session).list(
            limit=limit, status="all" if include_inactive else "active"
        )
        if not patrons:
            typer.echo("No patrons registered.")
            return
        for p in patrons:
            status = "" if p.is_active else " [inactive]"
            typer.echo(f"  {p.library_card_number}  {p.full_name}{status}")
