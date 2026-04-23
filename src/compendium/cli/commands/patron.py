import getpass
from datetime import date, datetime

import typer

from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_category_repository import (
    SqlPatronCategoryRepository,
)
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.audit import AuditService
from compendium.services.patrons import PatronService

app = typer.Typer(help="Patron management commands.")


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


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


@app.command("add")
def add_patron(
    name: str = typer.Option(..., "--name", help="Patron's full name"),
    email: str | None = typer.Option(None, "--email"),
    phone: str | None = typer.Option(None, "--phone"),
    link_user: str | None = typer.Option(None, "--link-user", help="Username to link to this patron"),
    category: str | None = typer.Option(None, "--category", help="Patron category code (adult/child/staff/teacher)"),
    expires: str | None = typer.Option(None, "--expires", help="Card expiry date (YYYY-MM-DD)"),
) -> None:
    """Register a new patron."""
    try:
        with session_scope() as session:
            user_id: int | None = None
            if link_user:
                u = SqlUserRepository(session).get_by_username(link_user)
                if u is None:
                    typer.echo(f"Error: No user with username '{link_user}'", err=True)
                    raise typer.Exit(1)
                user_id = u.id
            category_id = _resolve_category_id(session, category)
            expires_at = _parse_date(expires) if expires else None
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
    if link_user:
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


@app.command("list")
def list_patrons(
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """List registered patrons."""
    with session_scope() as session:
        patrons = SqlPatronRepository(session).list(limit=limit)
        if not patrons:
            typer.echo("No patrons registered.")
            return
        for p in patrons:
            status = "" if p.is_active else " [inactive]"
            typer.echo(f"  {p.library_card_number}  {p.full_name}{status}")
