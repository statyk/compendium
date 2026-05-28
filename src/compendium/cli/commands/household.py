"""Household management CLI commands."""
from __future__ import annotations

import getpass

import typer

from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.household_repository import SqlHouseholdRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.households import HouseholdService

app = typer.Typer(help="Household management commands.")


def _svc(session) -> HouseholdService:
    return HouseholdService(
        household_repo=SqlHouseholdRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


@app.command("create")
def create_household(
    name: str = typer.Option(..., "--name", "-n", help="Household display name"),
    notes: str | None = typer.Option(None, "--notes", help="Optional notes"),
) -> None:
    """Create a new household."""
    try:
        with session_scope() as session:
            hh = _svc(session).create(name=name, notes=notes)
        typer.echo(f"Created household {hh.id}: {hh.name}")
    except DomainError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command("list")
def list_households(
    limit: int = typer.Option(20, "--limit", "-l", help="Max results"),
) -> None:
    """List all households."""
    with session_scope() as session:
        svc = _svc(session)
        households = svc.list(limit=limit)
        patron_repo = SqlPatronRepository(session)
        if not households:
            typer.echo("No households found.")
            return
        for hh in households:
            count = len(patron_repo.list_by_household(hh.id))
            typer.echo(f"  [{hh.id}] {hh.name}  ({count} member{'s' if count != 1 else ''})")


@app.command("show")
def show_household(
    id: int = typer.Option(..., "--id", help="Household ID"),
) -> None:
    """Show household details and member list."""
    try:
        with session_scope() as session:
            svc = _svc(session)
            hh = svc.get(id)
            members = svc.get_members(id)
            loan_repo = SqlLoanRepository(session)
            hold_repo = SqlHoldRepository(session)
            typer.echo(f"Household [{hh.id}]: {hh.name}")
            if hh.notes:
                typer.echo(f"  Notes: {hh.notes}")
            typer.echo(f"  Members ({len(members)}):")
            for m in members:
                loans = loan_repo.count_for_patron(m.id)
                holds = hold_repo.count_active(patron_id=m.id)
                status = "active" if m.is_active else "inactive"
                typer.echo(
                    f"    [{m.library_card_number}] {m.full_name} "
                    f"({status}, {loans} loan{'s' if loans != 1 else ''}, "
                    f"{holds} hold{'s' if holds != 1 else ''})"
                )
    except DomainError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command("rename")
def rename_household(
    id: int = typer.Option(..., "--id", help="Household ID"),
    name: str = typer.Option(..., "--name", "-n", help="New display name"),
) -> None:
    """Rename a household."""
    try:
        with session_scope() as session:
            hh = _svc(session).update(id, name=name)
        typer.echo(f"Household {id} renamed to: {hh.name}")
    except DomainError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command("delete")
def delete_household(
    id: int = typer.Option(..., "--id", help="Household ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a household. All member links are removed first."""
    if not yes:
        confirmed = typer.confirm(
            f"Delete household {id}? Members will be unlinked but not deleted."
        )
        if not confirmed:
            raise typer.Abort()
    try:
        with session_scope() as session:
            _svc(session).delete(id)
        typer.echo(f"Household {id} deleted.")
    except DomainError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command("add-member")
def add_member(
    id: int = typer.Option(..., "--id", help="Household ID"),
    card: str = typer.Option(..., "--card", "-c", help="Patron library card number"),
) -> None:
    """Add a patron to a household."""
    try:
        with session_scope() as session:
            patron = _svc(session).add_member(id, card)
        typer.echo(f"Added {patron.full_name} ({card}) to household {id}.")
    except DomainError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@app.command("remove-member")
def remove_member(
    id: int = typer.Option(..., "--id", help="Household ID"),
    card: str = typer.Option(..., "--card", "-c", help="Patron library card number"),
) -> None:
    """Remove a patron from a household."""
    try:
        with session_scope() as session:
            patron = _svc(session).remove_member(id, card)
        typer.echo(f"Removed {patron.full_name} ({card}) from household {id}.")
    except DomainError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
