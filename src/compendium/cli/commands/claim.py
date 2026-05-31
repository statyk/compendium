import getpass

import typer

from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.circulation import CirculationService
from compendium.services.fines import FineService
from compendium.services.site_settings import get_site_setting

app = typer.Typer(help="Claims-returned dispute commands.")


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
        hold_pickup_days=get_site_setting("hold_pickup_days"),
        fine_svc=fines,
        audit_svc=audit,
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
        item_note_repo=SqlItemNoteRepository(session),
    )


@app.command("returned")
def returned(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
    note: str | None = typer.Option(None, "--note", help="Optional note on the claim"),
) -> None:
    """Mark an actively checked-out item as claims-returned (patron disputes)."""
    try:
        with session_scope() as session:
            item = _circulation(session).claim_returned(barcode, note=note)
            typer.echo(
                f"Item {item.barcode} marked claims-returned; loan remains open "
                f"pending investigation."
            )
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("verify")
def verify_returned(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
) -> None:
    """Resolve a claims-returned item as verified (found on shelf). Closes the loan."""
    try:
        with session_scope() as session:
            loan = _circulation(session).verify_returned(barcode)
            typer.echo(f"Item {barcode} verified returned; loan {loan.id} closed.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("write-off")
def write_off(
    barcode: str = typer.Option(..., "--barcode"),
    note: str = typer.Option(..., "--note", help="Required note explaining the write-off"),
) -> None:
    """Resolve a claims-returned item as written off. Closes the loan without
    replacement fine; existing fines are NOT waived automatically."""
    try:
        with session_scope() as session:
            loan = _circulation(session).write_off_claim(barcode, note=note)
            typer.echo(
                f"Claim on {barcode} written off; loan {loan.id} closed. "
                f"Existing fines (if any) remain — waive separately if desired."
            )
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("list")
def list_claims(
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List items currently in claims-returned status (outstanding investigations)."""
    from compendium.domain.enums import ItemStatus
    from compendium.domain.models import Item

    with session_scope() as session:
        items = (
            session.query(Item)
            .filter(Item.status == ItemStatus.CLAIMS_RETURNED.value)
            .order_by(Item.id)
            .limit(limit)
            .all()
        )
        if not items:
            typer.echo("No active claims-returned items.")
            return
        for item in items:
            loan = SqlLoanRepository(session).get_active_for_item(item.id)
            if loan is None:
                typer.echo(f"  {item.barcode}  (no active loan — desync?)")
                continue
            card = loan.patron.library_card_number if loan.patron else "?"
            title = item.work.title if item.work else "?"
            typer.echo(
                f"  {item.barcode}  loan={loan.id}  card={card}  title={title}"
            )
