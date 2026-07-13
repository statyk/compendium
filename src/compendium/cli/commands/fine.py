"""Fine CLI commands: list, pay, waive, assess, assess-overdue."""

from __future__ import annotations

import getpass

import typer

from compendium.cli.output import Column, emit_list, format_option
from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.domain.enums import FineKind
from compendium.domain.errors import DomainError, NotFoundError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.fines import FineService
from compendium.services.formatting import format_currency

app = typer.Typer(help="Fine management commands.")


def _fine_svc(session) -> FineService:
    return FineService(
        fine_repo=SqlFineRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        settings=get_settings(),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


def _resolve_patron(session, card: str):
    patron = SqlPatronRepository(session).get_by_card_number(card)
    if patron is None:
        raise NotFoundError(f"No patron with card number '{card}'")
    return patron


@app.command("list")
def list_fines(
    patron: str | None = typer.Option(None, "--patron", help="Filter by library card."),
    status: str | None = typer.Option(None, "--status", help="outstanding | paid | waived"),
    limit: int = typer.Option(50, "--limit"),
    format: str = format_option(),
) -> None:
    """List fines, optionally filtered."""
    with session_scope() as session:
        patron_id = None
        if patron:
            p = _resolve_patron(session, patron)
            patron_id = p.id
        fines = _fine_svc(session).list(patron_id=patron_id, status=status, limit=limit)
        emit_list(
            [{
                "id": f.id,
                "kind": f.kind,
                "amount_cents": f.amount_cents,
                "status": f.status,
                "patron_id": f.patron_id,
                "note": f.note,
            } for f in fines],
            [Column("id", "Fine", justify="right"),
             Column("kind", "Kind"),
             Column("amount_cents", "Amount", justify="right",
                    formatter=lambda v: format_currency(v)),
             Column("status", "Status"),
             Column("patron_id", "Patron ID", justify="right"),
             Column("note", "Note", formatter=lambda v: v or "")],
            format,
            empty="No fines matching filter.",
        )


@app.command("pay")
def pay_fine(
    fine_id: int = typer.Option(..., "--id", help="Fine ID to mark paid"),
) -> None:
    """Mark a fine as paid (full amount; no partial payments)."""
    try:
        with session_scope() as session:
            fine = _fine_svc(session).pay(fine_id)
            typer.echo(
                f"Fine #{fine.id} paid "
                f"({format_currency(fine.amount_cents)} {fine.kind})."
            )
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("waive")
def waive_fine(
    fine_id: int = typer.Option(..., "--id", help="Fine ID to waive"),
    note: str = typer.Option(..., "--note", help="Reason for waiving (required)"),
) -> None:
    """Waive a fine. Requires a note explaining why."""
    try:
        with session_scope() as session:
            fine = _fine_svc(session).waive(fine_id, note)
            typer.echo(
                f"Fine #{fine.id} waived "
                f"({format_currency(fine.amount_cents)} {fine.kind})."
            )
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("assess")
def assess_fine(
    patron: str = typer.Option(..., "--patron", help="Library card number"),
    kind: str = typer.Option(..., "--kind", help="overdue | lost | damaged | processing | other"),
    amount_cents: int = typer.Option(..., "--amount-cents"),
    note: str | None = typer.Option(None, "--note"),
    reason: str | None = typer.Option(None, "--reason"),
) -> None:
    """Manually assess a fine against a patron."""
    try:
        with session_scope() as session:
            p = _resolve_patron(session, patron)
            fine = _fine_svc(session).assess_manual(
                p, kind=kind, amount_cents=amount_cents, note=note, reason=reason
            )
            typer.echo(
                f"Assessed fine #{fine.id}: "
                f"{format_currency(fine.amount_cents)} ({fine.kind}) for {patron}."
            )
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("assess-overdue")
def assess_overdue_for_patron(
    patron: str = typer.Option(..., "--patron", help="Library card number"),
) -> None:
    """Materialize outstanding overdue fines for one patron."""
    try:
        with session_scope() as session:
            p = _resolve_patron(session, patron)
            counts = _fine_svc(session).assess_overdue_fines(patron_id=p.id)
            typer.echo(
                f"Overdue fines for {patron}: "
                f"created={counts['created']}, updated={counts['updated']}, "
                f"unchanged={counts['unchanged']}."
            )
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
