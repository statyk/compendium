import getpass

import typer

from compendium.cli.io import error, truncation_notice
from compendium.cli.output import Column, emit_list, format_option
from compendium.services.site_settings import get_site_setting
from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.domain.errors import AmbiguousItemError, DomainError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.circulation import CirculationService
from compendium.services.fines import FineService


app = typer.Typer(help="Loan (checkout / checkin) commands.")


def _loan_row(loan) -> dict:
    from datetime import datetime, timezone

    return {
        "id": loan.id,
        "patron_card": loan.patron.library_card_number,
        "item_barcode": loan.item.barcode,
        "title": loan.item.work.title,
        "checked_out_at": loan.checked_out_at,
        "due_at": loan.due_at,
        "returned_at": loan.returned_at,
        "renewal_count": loan.renewal_count,
        "overdue": loan.returned_at is None
        and loan.due_at < datetime.now(timezone.utc),
    }


_DATE = lambda v: v.strftime("%Y-%m-%d") if v else "—"  # noqa: E731

_LOAN_COLUMNS = [
    Column("id", "Loan", justify="right"),
    Column("patron_card", "Card"),
    Column("item_barcode", "Barcode"),
    Column("title", "Title"),
    Column("checked_out_at", "Out", formatter=_DATE),
    Column("due_at", "Due", formatter=_DATE),
    Column("returned_at", "Returned",
           formatter=lambda v: v.strftime("%Y-%m-%d") if v else "open"),
    Column("overdue", "Overdue", formatter=lambda v: "YES" if v else ""),
]


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
        work_repo=SqlWorkRepository(session),
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
        error(exc)
        raise typer.Exit(1) from exc


@app.command("checkin")
def checkin(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
) -> None:
    """Check an item back in."""
    try:
        with session_scope() as session:
            try:
                loan = _circulation(session).checkin(barcode)
                typer.echo(f"\nChecked in: {loan.item.work.title}")
                typer.echo(f"  Barcode : {loan.item.barcode}")
                card_num = loan.patron.library_card_number
                typer.echo(f"  Was on loan to: {loan.patron.full_name} ({card_num})")
            except AmbiguousItemError as exc:
                # Read all loan attributes while the session is still open —
                # rollback()/close() on scope exit expire and detach the ORM
                # objects, so they can't be rendered after the with-block.
                error(exc)
                typer.echo("Copies currently on loan:", err=True)
                for candidate in exc.loans:
                    due = candidate.due_at.strftime("%Y-%m-%d")
                    typer.echo(
                        f"  barcode={candidate.item.barcode}  accession={candidate.item.accession_number}  "
                        f"patron={candidate.patron.library_card_number}  due={due}",
                        err=True,
                    )
                typer.echo("Re-run with the copy's --barcode.", err=True)
                raise typer.Exit(1) from exc
    except DomainError as exc:
        error(exc)
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
        error(exc)
        raise typer.Exit(1) from exc


@app.command("active")
def active_loans(
    card: str = typer.Option(..., "--card", help="Patron library card number"),
    format: str = format_option(),
) -> None:
    """List active loans for a patron."""
    with session_scope() as session:
        patron = SqlPatronRepository(session).get_by_card_number(card)
        if patron is None:
            error(f"No patron with card '{card}'.")
            raise typer.Exit(1)
        loans = SqlLoanRepository(session).get_active_for_patron(patron.id)
        emit_list(
            [_loan_row(loan) for loan in loans], _LOAN_COLUMNS, format,
            empty=f"{patron.full_name} has no active loans.",
        )


@app.command("list")
def list_loans(
    due: str | None = typer.Option(None, "--due", help="Filter: overdue / due_soon / on_time"),
    branch: str | None = typer.Option(None, "--branch", help="Filter: branch code"),
    query: str | None = typer.Option(None, "--query", "-q", help="Search patron / barcode / title"),
    limit: int = typer.Option(100, "--limit"),
    format: str = format_option(),
) -> None:
    """System-wide active loans (librarian view). For a single patron's loans use 'active --card X'."""
    with session_scope() as session:
        branch_id: int | None = None
        if branch is not None:
            b = SqlBranchRepository(session).get_by_code(branch)
            if b is None:
                error(f"No branch with code '{branch}'.")
                raise typer.Exit(1)
            branch_id = b.id
        loans = SqlLoanRepository(session).list_active(
            due=due, branch_id=branch_id, query=query, limit=limit
        )
        emit_list([_loan_row(loan) for loan in loans], _LOAN_COLUMNS, format,
                  empty="No matching loans.")
        truncation_notice(len(loans), limit)


@app.command("history")
def patron_history(
    card: str = typer.Option(..., "--card", help="Patron library card number"),
    status: str = typer.Option("all", "--status", help="active / returned / all"),
    limit: int = typer.Option(100, "--limit"),
    format: str = format_option(),
) -> None:
    """Loan history for a patron (active, returned, or all)."""
    if status not in ("active", "returned", "all"):
        error("--status must be active, returned, or all.")
        raise typer.Exit(1)
    with session_scope() as session:
        patron = SqlPatronRepository(session).get_by_card_number(card)
        if patron is None:
            error(f"No patron with card '{card}'.")
            raise typer.Exit(1)
        loans = SqlLoanRepository(session).list_for_patron(
            patron.id, status=status, limit=limit
        )
        emit_list(
            [_loan_row(loan) for loan in loans], _LOAN_COLUMNS, format,
            empty=f"{patron.full_name} has no {status} loans.",
        )
        truncation_notice(len(loans), limit)


@app.command("item-history")
def item_history(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
    limit: int = typer.Option(25, "--limit"),
    format: str = format_option(),
) -> None:
    """Loan history for a specific copy."""
    with session_scope() as session:
        item = SqlItemRepository(session).get_by_barcode(barcode)
        if item is None:
            error(f"No item with barcode '{barcode}'.")
            raise typer.Exit(1)
        loans = SqlLoanRepository(session).list_for_item(item.id, limit=limit)
        emit_list(
            [_loan_row(loan) for loan in loans], _LOAN_COLUMNS, format,
            empty=f"Item {barcode} has no loan history.",
        )
