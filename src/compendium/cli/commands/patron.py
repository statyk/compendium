import random
import string

import typer

from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.domain.models import Patron
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.patrons import PatronService

app = typer.Typer(help="Patron management commands.")


def _generate_card_number() -> str:
    """Generate a random 8-digit library card number."""
    return "".join(random.choices(string.digits, k=8))


@app.command("add")
def add_patron(
    name: str = typer.Option(..., "--name", help="Patron's full name"),
    email: str | None = typer.Option(None, "--email"),
    phone: str | None = typer.Option(None, "--phone"),
) -> None:
    """Register a new patron."""
    try:
        with session_scope() as session:
            repo = SqlPatronRepository(session)
            card = _generate_card_number()
            while repo.get_by_card_number(card) is not None:
                card = _generate_card_number()

            patron = Patron(
                library_card_number=card,
                full_name=name,
                contact_email=email,
                contact_phone=phone,
            )
            repo.add(patron)
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"\nPatron registered: {name}")
    typer.echo(f"  Card number : {card}")
    if email:
        typer.echo(f"  Email       : {email}")
    if phone:
        typer.echo(f"  Phone       : {phone}")


@app.command("deactivate")
def deactivate_patron(
    card: str = typer.Option(..., "--card", help="Patron library card number"),
) -> None:
    """Deactivate a patron account (cancels active holds; blocks if active loans exist)."""
    try:
        with session_scope() as session:
            svc = PatronService(
                patron_repo=SqlPatronRepository(session),
                loan_repo=SqlLoanRepository(session),
                hold_repo=SqlHoldRepository(session),
            )
            patron = svc.deactivate(card)
            typer.echo(f"\nDeactivated: {patron.full_name} ({patron.library_card_number})")
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
