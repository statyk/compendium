"""Shared CLI output helpers: one table style, one JSON contract.

Every list/show command renders through these helpers. Rules:
- JSON to stdout only; humans' notices/warnings to stderr.
- JSON: full row dicts, snake_case keys, ISO-8601 UTC datetimes, integer
  cents, enum values as strings, no truncation.
- Tables: rich, ``box.SIMPLE``, bold headers. ``Column.formatter`` affects
  table rendering only — JSON always gets the raw value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Callable

import typer
from rich import box
from rich.console import Console
from rich.table import Table


class OutputFormat(str, Enum):
    JSON = "json"


def format_option(*, csv_ok: bool = False) -> Any:
    allowed = "table | csv | json" if csv_ok else "table | json"

    def _validate(value: str) -> str:
        v = (value or "").strip().lower()
        ok = {"table", "json", "csv"} if csv_ok else {"table", "json"}
        if v not in ok:
            raise typer.BadParameter(f"must be one of: {allowed}")
        return v

    return typer.Option(
        "table", "--format", help=f"Output format: {allowed}.", callback=_validate
    )


@dataclass
class Column:
    key: str
    header: str
    justify: str = "left"
    formatter: Callable[[Any], str] | None = None


def json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=timezone.utc)
        return o.astimezone(timezone.utc).isoformat()
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    raise TypeError(f"Not JSON serializable: {type(o).__name__}")


def _dump(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=json_default))


def _cell(row: dict, col: Column) -> str:
    value = row.get(col.key)
    if col.formatter is not None:
        return col.formatter(value)
    if value is None:
        return "—"
    return str(value)


def _table(columns: list[Column], title: str | None = None) -> Table:
    table = Table(box=box.SIMPLE, header_style="bold", title=title)
    for col in columns:
        table.add_column(col.header, justify=col.justify)  # type: ignore[arg-type]
    return table


def emit_list(
    rows: list[dict],
    columns: list[Column],
    fmt: str,
    *,
    empty: str = "Nothing to show.",
) -> None:
    if fmt == OutputFormat.JSON.value:
        _dump(rows)
        return
    if not rows:
        typer.echo(empty)
        return
    table = _table(columns)
    for row in rows:
        table.add_row(*(_cell(row, c) for c in columns))
    Console(highlight=False).print(table)


def emit_detail(obj: dict, fmt: str, *, title: str | None = None) -> None:
    if fmt == OutputFormat.JSON.value:
        _dump(obj)
        return
    if title:
        typer.echo(title)
    width = max((len(k) for k in obj), default=0)
    for key, value in obj.items():
        shown = "—" if value is None else value
        typer.echo(f"  {key:<{width}} : {shown}")


def emit_csv(rows: list[dict], fieldnames: list[str]) -> None:
    import csv as _csv
    import io

    from compendium.services.import_export import csv_safe_cell

    buf = io.StringIO()
    writer = _csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: csv_safe_cell(row.get(k)) for k in fieldnames})
    typer.echo(buf.getvalue(), nl=False)
