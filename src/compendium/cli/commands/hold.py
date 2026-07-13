import getpass
from datetime import datetime

import typer

from compendium.cli.io import error, truncation_notice
from compendium.cli.output import Column, emit_list, format_option
from compendium.services.site_settings import get_site_setting
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


def _hold_row(hold, queue_position=None) -> dict:
    row = {
        "id": hold.id,
        "work_id": hold.work_id,
        "title": hold.work.title if hold.work else None,
        "patron_card": hold.patron.library_card_number,
        "status": hold.status,
        "placed_at": hold.placed_at,
        "expires_at": hold.expires_at,
        "suspended_until": hold.suspended_until,
        "suspended_reason": hold.suspended_reason,
    }
    if queue_position is not None:
        row["queue_position"] = queue_position
    return row


_DATE = lambda v: v.strftime("%Y-%m-%d") if v else "—"  # noqa: E731

_HOLD_COLUMNS = [
    Column("id", "Hold", justify="right"),
    Column("title", "Title"),
    Column("patron_card", "Card"),
    Column("status", "Status"),
    Column("expires_at", "Expires", formatter=_DATE),
    Column("suspended_until", "Susp. until",
           formatter=lambda v: v.isoformat() if v else ""),
]


def _holds(session) -> HoldService:
    settings = get_settings()
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
        hold_expiry_days=get_site_setting("hold_expiry_days"),
        hold_pickup_days=get_site_setting("hold_pickup_days"),
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
        error(exc)
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
                error(f"No patron with card '{card}'.")
                raise typer.Exit(1)
            hold = _holds(session).cancel(hold_id, patron.id)
            typer.echo(f"Hold {hold.id} cancelled.")
    except DomainError as exc:
        error(exc)
        raise typer.Exit(1) from exc


@app.command("list")
def list_holds(
    card: str | None = typer.Option(None, "--card", help="Patron library card number. Omit to list all active holds (librarian view)."),
    status: str | None = typer.Option(None, "--status", help="Filter: waiting / available"),
    branch: str | None = typer.Option(None, "--branch", help="Filter: branch code"),
    older_than_days: int | None = typer.Option(None, "--older-than-days", help="Only holds placed more than N days ago"),
    query: str | None = typer.Option(None, "--query", "-q", help="Search patron name/card or work title"),
    limit: int = typer.Option(100, "--limit"),
    format: str = format_option(),
) -> None:
    """List active holds.

    With --card: patron-scoped view.
    Without --card: system-wide active holds for librarian review.
    """
    with session_scope() as session:
        if card is not None:
            patron = SqlPatronRepository(session).get_by_card_number(card)
            if patron is None:
                error(f"No patron with card '{card}'.")
                raise typer.Exit(1)
            holds = SqlHoldRepository(session).get_active_for_patron(patron.id)
            empty = f"{patron.full_name} has no active holds."
        else:
            branch_id: int | None = None
            if branch is not None:
                b = SqlBranchRepository(session).get_by_code(branch)
                if b is None:
                    error(f"No branch with code '{branch}'.")
                    raise typer.Exit(1)
                branch_id = b.id
            holds = SqlHoldRepository(session).list_active(
                status=status,
                branch_id=branch_id,
                query=query,
                older_than_days=older_than_days,
                limit=limit,
            )
            empty = "No matching holds."
        emit_list([_hold_row(h) for h in holds], _HOLD_COLUMNS, format, empty=empty)
        truncation_notice(len(holds), limit)


@app.command("queue")
def queue_cmd(
    work_id: int = typer.Option(..., "--work-id", help="Work ID"),
    format: str = format_option(),
) -> None:
    """Show the hold queue for a work (ordered by placement)."""
    with session_scope() as session:
        holds = SqlHoldRepository(session).queue_for_work(work_id)
        rows = [_hold_row(h, pos) for pos, h in enumerate(holds, start=1)]
        columns = [Column("queue_position", "#", justify="right")] + _HOLD_COLUMNS
        emit_list(rows, columns, format, empty=f"No active holds on work {work_id}.")


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
        error("--until must be YYYY-MM-DD")
        raise typer.Exit(1)
    try:
        with session_scope() as session:
            hold = _holds(session).suspend(hold_id, until=parsed, reason=reason)
            typer.echo(f"Hold #{hold.id} suspended until {parsed.isoformat()}.")
    except DomainError as exc:
        error(exc)
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
        error(exc)
        raise typer.Exit(1) from exc


@app.command("list-suspended")
def list_suspended(format: str = format_option()) -> None:
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
        emit_list(
            [_hold_row(h) for h in rows],
            [
                Column("id", "Hold", justify="right"),
                Column("work_id", "Work", justify="right"),
                Column("patron_card", "Card"),
                Column("suspended_until",
                       "Susp. until", formatter=lambda v: v.isoformat() if v else ""),
                Column("suspended_reason", "Reason", formatter=lambda v: v or ""),
            ],
            format,
            empty="No suspended holds.",
        )
