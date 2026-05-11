"""Circulation & overdue reports CLI."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import typer

from compendium.db.session import session_scope
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.import_export import csv_safe_cell
from compendium.services.reports import ReportsService

app = typer.Typer(help="Circulation & overdue reports.")


def _svc(session) -> ReportsService:
    return ReportsService(
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
    )


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _emit_csv(rows: list[dict], fieldnames: list[str]) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: csv_safe_cell(v) for k, v in row.items()})
    typer.echo(buf.getvalue(), nl=False)


@app.command("checkouts")
def checkouts(
    months: int = typer.Option(12, "--months", help="How many months back, inclusive"),
    branch: str | None = typer.Option(None, "--branch", help="Branch code filter"),
    format: str = typer.Option("table", "--format", help="table | csv"),
) -> None:
    """Checkouts per month."""
    with session_scope() as session:
        rows = _svc(session).checkouts_per_month(months=months, branch_code=branch)
    if format == "csv":
        _emit_csv(
            [{"month": r.month, "count": r.count} for r in rows],
            ["month", "count"],
        )
        return
    typer.echo(f"{'Month':>9}  {'Count':>6}")
    typer.echo("-" * 18)
    for r in rows:
        typer.echo(f"{r.month:>9}  {r.count:>6}")


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
    format: str = typer.Option("table", "--format"),
) -> None:
    """Most-checked-out works in a date window."""
    since_dt = _parse_date(since)
    until_dt = _parse_date(until) if until else None
    with session_scope() as session:
        rows = _svc(session).popular_works(
            since=since_dt, until=until_dt, limit=limit, branch_code=branch
        )
    if format == "csv":
        _emit_csv(
            [
                {
                    "work_id": r.work_id,
                    "title": r.title,
                    "media_type": r.media_type_code,
                    "checkout_count": r.checkout_count,
                }
                for r in rows
            ],
            ["work_id", "title", "media_type", "checkout_count"],
        )
        return
    typer.echo(f"{'Count':>6}  {'Media':8}  Title")
    typer.echo("-" * 60)
    for r in rows:
        typer.echo(f"{r.checkout_count:>6}  {r.media_type_code:8}  {r.title}")


@app.command("dormant")
def dormant(
    not_since: str = typer.Option(
        ...,
        "--not-since",
        help="Items not checked out since this date (YYYY-MM-DD)",
    ),
    limit: int = typer.Option(100, "--limit"),
    branch: str | None = typer.Option(None, "--branch"),
    format: str = typer.Option("table", "--format"),
) -> None:
    """Items not checked out since a cutoff date — weeding list."""
    cutoff = _parse_date(not_since)
    with session_scope() as session:
        rows = _svc(session).dormant_items(
            not_since=cutoff, limit=limit, branch_code=branch
        )
    if format == "csv":
        _emit_csv(
            [
                {
                    "barcode": r.barcode,
                    "title": r.title,
                    "media_type": r.media_type_code,
                    "branch": r.branch_code,
                    "last_checkout": r.last_checkout_at.strftime("%Y-%m-%d")
                    if r.last_checkout_at
                    else "",
                }
                for r in rows
            ],
            ["barcode", "title", "media_type", "branch", "last_checkout"],
        )
        return
    typer.echo(f"{'Barcode':16}  {'Last checkout':13}  {'Media':8}  Title")
    typer.echo("-" * 70)
    for r in rows:
        last = r.last_checkout_at.strftime("%Y-%m-%d") if r.last_checkout_at else "never"
        typer.echo(f"{r.barcode:16}  {last:13}  {r.media_type_code:8}  {r.title}")


@app.command("overdues")
def overdues(
    branch: str | None = typer.Option(None, "--branch"),
    format: str = typer.Option("table", "--format"),
) -> None:
    """Active overdue loans across all patrons."""
    with session_scope() as session:
        rows = _svc(session).current_overdues(branch_code=branch)
    if format == "csv":
        _emit_csv(
            [
                {
                    "loan_id": r.loan_id,
                    "patron_card": r.patron_card,
                    "patron_name": r.patron_name,
                    "item_barcode": r.item_barcode,
                    "title": r.title,
                    "due_at": r.due_at.strftime("%Y-%m-%d"),
                    "days_overdue": r.days_overdue,
                }
                for r in rows
            ],
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
    typer.echo(
        f"{'Days':>4}  {'Due':10}  {'Card':10}  {'Patron':20}  {'Barcode':16}  Title"
    )
    typer.echo("-" * 90)
    for r in rows:
        due = r.due_at.strftime("%Y-%m-%d")
        typer.echo(
            f"{r.days_overdue:>4}  {due:10}  {r.patron_card:10}  "
            f"{r.patron_name[:20]:20}  {r.item_barcode:16}  {r.title}"
        )
