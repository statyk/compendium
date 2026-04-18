import typer

from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.holds import HoldService

app = typer.Typer(help="Hold (reservation) commands.")


def _holds(session) -> HoldService:
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_expiry_days=get_settings().hold_expiry_days,
    )


@app.command("place")
def place(
    work_id: int = typer.Option(..., "--work-id", help="Work ID to place hold on"),
    card: str = typer.Option(..., "--card", help="Patron library card number"),
) -> None:
    """Place a hold on a work for a patron."""
    try:
        with session_scope() as session:
            hold = _holds(session).place(work_id, card)
            typer.echo("\nHold placed:")
            typer.echo(f"  Hold ID : {hold.id}")
            typer.echo(f"  Work ID : {hold.work_id}")
            typer.echo(f"  Patron  : {hold.patron.full_name} ({hold.patron.library_card_number})")
            typer.echo(f"  Status  : {hold.status}")
            if hold.expires_at:
                typer.echo(f"  Expires : {hold.expires_at.strftime('%Y-%m-%d')}")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("cancel")
def cancel(
    hold_id: int = typer.Option(..., "--id", help="Hold ID to cancel"),
    card: str = typer.Option(..., "--card", help="Patron library card number"),
) -> None:
    """Cancel a hold."""
    try:
        with session_scope() as session:
            patron = SqlPatronRepository(session).get_by_card_number(card)
            if patron is None:
                typer.echo(f"No patron with card '{card}'.", err=True)
                raise typer.Exit(1)
            hold = _holds(session).cancel(hold_id, patron.id)
            typer.echo(f"Hold {hold.id} cancelled.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("list")
def list_holds(
    card: str = typer.Option(..., "--card", help="Patron library card number"),
) -> None:
    """List active holds for a patron."""
    with session_scope() as session:
        patron = SqlPatronRepository(session).get_by_card_number(card)
        if patron is None:
            typer.echo(f"No patron with card '{card}'.", err=True)
            raise typer.Exit(1)
        holds = SqlHoldRepository(session).get_active_for_patron(patron.id)
        if not holds:
            typer.echo(f"{patron.full_name} has no active holds.")
            return
        typer.echo(f"\nActive holds for {patron.full_name}:")
        for hold in holds:
            exp = hold.expires_at.strftime("%Y-%m-%d") if hold.expires_at else "—"
            typer.echo(f"  #{hold.id}  Work {hold.work_id}  status={hold.status}  expires={exp}")
