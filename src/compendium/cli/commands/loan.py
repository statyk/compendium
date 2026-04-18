import typer

from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.circulation import CirculationService

app = typer.Typer(help="Loan (checkout / checkin) commands.")


def _circulation(session) -> CirculationService:
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        loan_period_days=get_settings().default_loan_period_days,
    )


@app.command("checkout")
def checkout(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
    card: str = typer.Option(..., "--card", help="Patron library card number"),
) -> None:
    """Check an item out to a patron."""
    try:
        with session_scope() as session:
            loan = _circulation(session).checkout(barcode, card)
            typer.echo(f"\nChecked out: {loan.item.work.title}")
            typer.echo(f"  Barcode : {loan.item.barcode}")
            typer.echo(f"  Patron  : {loan.patron.full_name} ({loan.patron.library_card_number})")
            typer.echo(f"  Due     : {loan.due_at.strftime('%Y-%m-%d')}")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


@app.command("checkin")
def checkin(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
) -> None:
    """Check an item back in."""
    try:
        with session_scope() as session:
            loan = _circulation(session).checkin(barcode)
            typer.echo(f"\nChecked in: {loan.item.work.title}")
            typer.echo(f"  Barcode : {loan.item.barcode}")
            typer.echo(f"  Was on loan to: {loan.patron.full_name} ({loan.patron.library_card_number})")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)


@app.command("active")
def active_loans(
    card: str = typer.Option(..., "--card", help="Patron library card number"),
) -> None:
    """List active loans for a patron."""
    with session_scope() as session:
        patron = SqlPatronRepository(session).get_by_card_number(card)
        if patron is None:
            typer.echo(f"No patron with card '{card}'.", err=True)
            raise typer.Exit(1)
        loans = SqlLoanRepository(session).get_active_for_patron(patron.id)
        if not loans:
            typer.echo(f"{patron.full_name} has no active loans.")
            return
        typer.echo(f"\nActive loans for {patron.full_name}:")
        for loan in loans:
            overdue = ""
            from datetime import datetime
            if loan.due_at < datetime.utcnow():
                overdue = "  *** OVERDUE ***"
            typer.echo(f"  {loan.item.work.title}  |  due {loan.due_at.strftime('%Y-%m-%d')}{overdue}")
