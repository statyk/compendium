"""Circulation & overdue reports CLI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import typer

from compendium.cli.io import truncation_notice
from compendium.cli.output import Column, emit_csv, emit_list, format_option
from compendium.db.session import session_scope
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.reports import ReportsService

app = typer.Typer(help="Circulation & overdue reports.")


def _svc(session) -> ReportsService:
    return ReportsService(
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
    )


def _parse_date(s: str, opt: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise typer.BadParameter(f"{opt} must be YYYY-MM-DD, got '{s}'") from exc


@app.command("checkouts")
def checkouts(
    months: int = typer.Option(12, "--months", help="How many months back, inclusive"),
    branch: str | None = typer.Option(None, "--branch", help="Branch code filter"),
    format: str = format_option(csv_ok=True),
) -> None:
    """Checkouts per month."""
    with session_scope() as session:
        results = _svc(session).checkouts_per_month(months=months, branch_code=branch)
    rows = [{"month": r.month, "count": r.count} for r in results]
    if format == "csv":
        emit_csv(rows, ["month", "count"])
        return
    emit_list(
        rows,
        [
            Column("month", "Month", justify="right"),
            Column("count", "Count", justify="right"),
        ],
        format,
        empty="No checkouts in window.",
    )


@app.command("popular")
def popular(
    since: str = typer.Option(
        ...,
        "--from",
        help="Window start (YYYY-MM-DD)",
    ),
    until: str | None = typer.Option(None, "--to", help="Window end (YYYY-MM-DD)"),
    limit: int = typer.Option(20, "--limit"),
    branch: str | None = typer.Option(None, "--branch"),
    format: str = format_option(csv_ok=True),
) -> None:
    """Most-checked-out works in a date window."""
    since_dt = _parse_date(since, "--from")
    until_dt = _parse_date(until, "--to") if until else None
    with session_scope() as session:
        results = _svc(session).popular_works(
            since=since_dt, until=until_dt, limit=limit, branch_code=branch
        )
    rows = [
        {
            "work_id": r.work_id,
            "title": r.title,
            "media_type": r.media_type_code,
            "checkout_count": r.checkout_count,
        }
        for r in results
    ]
    if format == "csv":
        emit_csv(rows, ["work_id", "title", "media_type", "checkout_count"])
        truncation_notice(len(results), limit)
        return
    emit_list(
        rows,
        [
            Column("checkout_count", "Count", justify="right"),
            Column("media_type", "Media"),
            Column("title", "Title"),
        ],
        format,
        empty="No checkouts in window.",
    )
    truncation_notice(len(results), limit)


@app.command("dormant")
def dormant(
    not_since: str = typer.Option(
        ...,
        "--not-since",
        help="Items not checked out since this date (YYYY-MM-DD)",
    ),
    limit: int = typer.Option(100, "--limit"),
    branch: str | None = typer.Option(None, "--branch"),
    format: str = format_option(csv_ok=True),
) -> None:
    """Items not checked out since a cutoff date — weeding list."""
    cutoff = _parse_date(not_since, "--not-since")
    with session_scope() as session:
        results = _svc(session).dormant_items(
            not_since=cutoff, limit=limit, branch_code=branch
        )
    rows = [
        {
            "barcode": r.barcode,
            "title": r.title,
            "media_type": r.media_type_code,
            "branch": r.branch_code,
            "last_checkout": r.last_checkout_at.strftime("%Y-%m-%d")
            if r.last_checkout_at
            else "",
        }
        for r in results
    ]
    if format == "csv":
        emit_csv(rows, ["barcode", "title", "media_type", "branch", "last_checkout"])
        truncation_notice(len(results), limit)
        return
    emit_list(
        rows,
        [
            Column("barcode", "Barcode"),
            Column("last_checkout", "Last checkout", formatter=lambda v: v or "never"),
            Column("media_type", "Media"),
            Column("title", "Title"),
        ],
        format,
        empty="No dormant items.",
    )
    truncation_notice(len(results), limit)


@app.command("overdues")
def overdues(
    branch: str | None = typer.Option(None, "--branch"),
    format: str = format_option(csv_ok=True),
) -> None:
    """Active overdue loans across all patrons."""
    with session_scope() as session:
        results = _svc(session).current_overdues(branch_code=branch)
    rows = [
        {
            "loan_id": r.loan_id,
            "patron_card": r.patron_card,
            "patron_name": r.patron_name,
            "item_barcode": r.item_barcode,
            "title": r.title,
            "due_at": r.due_at.strftime("%Y-%m-%d"),
            "days_overdue": r.days_overdue,
        }
        for r in results
    ]
    if format == "csv":
        emit_csv(
            rows,
            [
                "loan_id",
                "patron_card",
                "patron_name",
                "item_barcode",
                "title",
                "due_at",
                "days_overdue",
            ],
        )
        return
    emit_list(
        rows,
        [
            Column("days_overdue", "Days", justify="right"),
            Column("due_at", "Due"),
            Column("patron_card", "Card"),
            Column("patron_name", "Patron"),
            Column("item_barcode", "Barcode"),
            Column("title", "Title"),
        ],
        format,
        empty="No overdue loans.",
    )
