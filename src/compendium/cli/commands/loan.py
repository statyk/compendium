import getpass

import typer

from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.circulation import CirculationService
from compendium.services.fines import FineService
from compendium.services.formatting import format_currency

app = typer.Typer(help="Loan (checkout / checkin) commands.")


def _circulation(session) -> CirculationService:
    settings = get_settings()
    audit = AuditService(SqlAuditLogRepository(session))
    fines = FineService(
        fine_repo=SqlFineRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        settings=settings,
        audit_svc=audit,
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        hold_pickup_days=settings.hold_pickup_days,
        fine_svc=fines,
        audit_svc=audit,
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


@app.command("checkout")
def checkout(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
    card: str = typer.Option(..., "--card", help="Patron library card number"),
    override_holds: bool = typer.Option(
        False,
        "--override-holds",
        help="Bypass the hold queue guard (audited). Use when deliberately "
        "jumping a waiting hold on behalf of this patron.",
    ),
) -> None:
    """Check an item out to a patron."""
    try:
        with session_scope() as session:
            loan = _circulation(session).checkout(
                barcode, card, override_holds=override_holds
            )
            typer.echo(f"\nChecked out: {loan.item.work.title}")
            typer.echo(f"  Barcode : {loan.item.barcode}")
            typer.echo(f"  Patron  : {loan.patron.full_name} ({loan.patron.library_card_number})")
            typer.echo(f"  Due     : {loan.due_at.strftime('%Y-%m-%d')}")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


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
            card_num = loan.patron.library_card_number
            typer.echo(f"  Was on loan to: {loan.patron.full_name} ({card_num})")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("renew")
def renew(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
    card: str = typer.Option(..., "--card", help="Patron library card number"),
) -> None:
    """Renew an active loan."""
    try:
        with session_scope() as session:
            loan = _circulation(session).renew(barcode, card)
            typer.echo(f"\nRenewed: {loan.item.work.title}")
            typer.echo(f"  Barcode  : {loan.item.barcode}")
            typer.echo(f"  New due  : {loan.due_at.strftime('%Y-%m-%d')}")
            typer.echo(f"  Renewals : {loan.renewal_count}")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("active")
def active_loans(
    card: str = typer.Option(..., "--card", help="Patron library card number"),
) -> None:
    """List active loans for a patron."""
    from datetime import datetime, timezone

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
            overdue = "  *** OVERDUE ***" if loan.due_at < datetime.now(timezone.utc) else ""
            due = loan.due_at.strftime("%Y-%m-%d")
            typer.echo(f"  {loan.item.work.title}  |  due {due}{overdue}")


@app.command("declare-lost")
def declare_lost(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
    replacement_cost_cents: int | None = typer.Option(
        None,
        "--replacement-cost-cents",
        help="Replacement cost (cents). Defaults to the policy's lost_item_default_cents.",
    ),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Declare an item lost. Closes any active loan, cancels pending holds,
    assesses lost + processing fees."""
    try:
        with session_scope() as session:
            item = _circulation(session).declare_lost(
                barcode, replacement_cost_cents=replacement_cost_cents, note=note
            )
            typer.echo(
                f"Item {item.barcode} declared lost "
                f"(replacement cost {format_currency(replacement_cost_cents or 0)})."
            )
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("mark-damaged")
def mark_damaged(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
    amount_cents: int = typer.Option(..., "--amount-cents"),
    note: str = typer.Option(..., "--note"),
) -> None:
    """Mark an item damaged. Assesses a damaged fee."""
    try:
        with session_scope() as session:
            item = _circulation(session).mark_damaged(
                barcode, amount_cents=amount_cents, note=note
            )
            typer.echo(f"Item {item.barcode} marked damaged ({format_currency(amount_cents)}).")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("clear-damage")
def clear_damage(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
) -> None:
    """Clear a damaged status and restore the item to AVAILABLE.
    (Any associated damaged-fee is not modified.)"""
    try:
        with session_scope() as session:
            item = _circulation(session).clear_damage(barcode)
            typer.echo(f"Item {item.barcode} cleared, now {item.status}.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("clear-lost")
def clear_lost(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
) -> None:
    """Clear a lost status and restore the item to AVAILABLE.
    (Any associated lost-fee is not modified.)"""
    try:
        with session_scope() as session:
            item = _circulation(session).clear_lost(barcode)
            typer.echo(f"Item {item.barcode} recovered, now {item.status}.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
