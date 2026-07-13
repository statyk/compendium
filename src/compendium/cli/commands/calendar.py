"""Calendar CLI commands: library hours and closed dates."""
from __future__ import annotations

import getpass
from datetime import date, time

import typer

from compendium.cli.output import Column, emit_detail, emit_list, format_option
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.calendar_repository import (
    SqlClosedDateRepository,
    SqlLibraryHoursRepository,
)
from compendium.services.audit import AuditService
from compendium.services.calendar import CalendarService
from compendium.services.site_settings import get_site_setting

app = typer.Typer(help="Library hours and closed-date calendar management.")
hours_app = typer.Typer(help="Weekly library hours schedule.")
closed_app = typer.Typer(help="Closed-date entries (holidays, breaks).")
app.add_typer(hours_app, name="hours")
app.add_typer(closed_app, name="closed-date")


_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _svc(session) -> CalendarService:
    return CalendarService(
        hours_repo=SqlLibraryHoursRepository(session),
        closed_date_repo=SqlClosedDateRepository(session),
        timezone=get_site_setting("library_timezone"),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


# ------------------------------------------------------------------
# Library Hours subcommands
# ------------------------------------------------------------------

@hours_app.command("show")
def hours_show(format: str = format_option()) -> None:
    """Show the weekly library hours schedule."""
    with session_scope() as session:
        rows = SqlLibraryHoursRepository(session).list()
        tz = get_site_setting("library_timezone")
        payload = {
            "timezone": tz,
            "days": [
                {
                    "weekday": h.weekday,
                    "weekday_name": _WEEKDAY_NAMES[h.weekday],
                    "is_open": h.is_open,
                    "open_time": h.open_time.strftime("%H:%M") if h.open_time else None,
                    "close_time": h.close_time.strftime("%H:%M") if h.close_time else None,
                }
                for h in rows
            ],
        }
        if format == "json":
            emit_detail(payload, format)
            return
        typer.echo(f"Library timezone: {tz}")
        typer.echo("")
        for h in rows:
            day = _WEEKDAY_NAMES[h.weekday]
            if not h.is_open:
                typer.echo(f"  {day:12s}  Closed")
            else:
                open_t = h.open_time.strftime("%H:%M") if h.open_time else "00:00"
                close_t = h.close_time.strftime("%H:%M") if h.close_time else "23:59"
                typer.echo(f"  {day:12s}  {open_t} – {close_t}")


@hours_app.command("set")
def hours_set(
    weekday: int = typer.Option(..., "--weekday", "-d",
                                 help="Weekday (0=Monday … 6=Sunday)"),
    is_open: bool = typer.Option(None, "--open/--closed"),
    open_time: str | None = typer.Option(None, "--open-time", help="HH:MM"),
    close_time: str | None = typer.Option(None, "--close-time", help="HH:MM"),
) -> None:
    """Update library hours for a single weekday."""
    try:
        parsed_open = _parse_time(open_time)
        parsed_close = _parse_time(close_time)
        with session_scope() as session:
            svc = _svc(session)
            svc.update_weekday(
                weekday,
                is_open=is_open,
                open_time=parsed_open if parsed_open is not None else ...,
                close_time=parsed_close if parsed_close is not None else ...,
            )
            typer.echo(f"Updated {_WEEKDAY_NAMES[weekday]} hours.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


# ------------------------------------------------------------------
# Closed Dates subcommands
# ------------------------------------------------------------------

@closed_app.command("list")
def closed_date_list(format: str = format_option()) -> None:
    """List upcoming and annually-recurring closed dates."""
    with session_scope() as session:
        dates = SqlClosedDateRepository(session).list(limit=200)
        rows = [
            {
                "id": cd.id,
                "start_date": cd.start_date,
                "end_date": cd.end_date,
                "recurs_annually": cd.recurs_annually,
                "label": cd.label,
            }
            for cd in dates
        ]
        emit_list(
            rows,
            [
                Column("id", "#", justify="right"),
                Column("start_date", "Start", formatter=lambda v: v.isoformat()),
                Column("end_date", "End", formatter=lambda v: v.isoformat()),
                Column("recurs_annually", "Recurs", formatter=lambda v: "annually" if v else ""),
                Column("label", "Label", formatter=lambda v: v or ""),
            ],
            format,
            empty="No closed dates defined.",
        )


@closed_app.command("add")
def closed_date_add(
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"),
    end: str | None = typer.Option(None, "--end", help="YYYY-MM-DD (defaults to start)"),
    label: str | None = typer.Option(None, "--label"),
    annually: bool = typer.Option(False, "--annually", help="Repeat every year"),
) -> None:
    """Add a closed-date entry."""
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end) if end else None
        with session_scope() as session:
            cd = _svc(session).add_closed_date(
                start_date, end_date, label=label, recurs_annually=annually
            )
            typer.echo(f"Added closed date id={cd.id}")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@closed_app.command("delete")
def closed_date_delete(
    id: int = typer.Option(..., "--id", help="Closed date row id"),
) -> None:
    """Delete a closed-date entry by id."""
    try:
        with session_scope() as session:
            _svc(session).delete_closed_date(id)
            typer.echo(f"Deleted closed date id={id}.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _parse_time(s: str | None) -> time | None:
    if not s:
        return None
    try:
        parts = s.split(":")
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError) as exc:
        raise ValueError(f"Invalid time '{s}' — expected HH:MM format.") from exc
