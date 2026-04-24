import getpass
from datetime import datetime

import typer

from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.holds import HoldService

app = typer.Typer(help="Hold (reservation) commands.")


def _holds(session) -> HoldService:
    settings = get_settings()
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
        hold_expiry_days=settings.hold_expiry_days,
        hold_pickup_days=settings.hold_pickup_days,
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
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
            susp = ""
            if hold.suspended_until is not None:
                susp = f"  suspended_until={hold.suspended_until.isoformat()}"
            typer.echo(
                f"  #{hold.id}  Work {hold.work_id}  status={hold.status}  expires={exp}{susp}"
            )


@app.command("suspend")
def suspend_hold(
    hold_id: int = typer.Option(..., "--id", help="Hold ID to suspend"),
    until: str = typer.Option(..., "--until", help="Resume date (YYYY-MM-DD)"),
    reason: str | None = typer.Option(None, "--reason", help="Optional free-text reason"),
) -> None:
    """Suspend a waiting hold until a date — the queue will skip it until then."""
    try:
        parsed = datetime.strptime(until, "%Y-%m-%d").date()
    except ValueError:
        typer.echo("Error: --until must be YYYY-MM-DD", err=True)
        raise typer.Exit(1)
    try:
        with session_scope() as session:
            hold = _holds(session).suspend(hold_id, until=parsed, reason=reason)
            typer.echo(f"Hold #{hold.id} suspended until {parsed.isoformat()}.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("resume")
def resume_hold(
    hold_id: int = typer.Option(..., "--id", help="Hold ID to resume"),
) -> None:
    """Clear the suspension on a hold; auto-promote if a copy is available."""
    try:
        with session_scope() as session:
            hold = _holds(session).resume(hold_id)
            if hold.status == "available":
                typer.echo(
                    f"Hold #{hold.id} resumed and immediately promoted — copy reserved."
                )
            else:
                typer.echo(f"Hold #{hold.id} resumed; back in the queue.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("list-suspended")
def list_suspended() -> None:
    """List all holds currently suspended (across all patrons)."""
    from datetime import date

    from compendium.domain.models import Hold

    with session_scope() as session:
        rows = (
            session.query(Hold)
            .filter(Hold.suspended_until.is_not(None), Hold.suspended_until > date.today())
            .order_by(Hold.suspended_until.asc())
            .all()
        )
        if not rows:
            typer.echo("No suspended holds.")
            return
        typer.echo(f"\n{len(rows)} suspended hold(s):")
        for hold in rows:
            until = hold.suspended_until.isoformat()
            reason = f" reason={hold.suspended_reason!r}" if hold.suspended_reason else ""
            typer.echo(
                f"  #{hold.id}  work={hold.work_id}  patron={hold.patron.library_card_number}  "
                f"until={until}{reason}"
            )
