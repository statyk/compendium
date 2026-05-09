"""CLI commands: bulk import/export of catalog data via CSV and MARC.

Module is named ``bulk_ops`` because ``import`` is a Python keyword; the Typer
apps below are registered at the top level as ``import`` and ``export``.
"""

from __future__ import annotations

import getpass
import io
import sys
from datetime import datetime
from pathlib import Path

import typer

from compendium.cli.io import is_stdio, open_input, open_output
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.repositories.sql.counters import SqlCounterRepository
from compendium.services.catalog import CatalogService
from compendium.services.import_export import (
    ExportFilters,
    ExportService,
    ImportMode,
    ImportOptions,
    ImportService,
    decode_text_bytes,
)

import_app = typer.Typer(help="Bulk import catalog data from MARC or CSV.")
export_app = typer.Typer(help="Bulk export catalog data to MARC or CSV.")


def _make_importer(session):
    catalog = CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=None,
        counter_repo=SqlCounterRepository(session),
    )
    return ImportService(
        session=session,
        catalog=catalog,
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


def _print_report(report, *, quiet: bool = False) -> None:
    typer.echo(f"\nImport report ({report.source}):")
    if report.filename:
        typer.echo(f"  file        : {report.filename}")
    typer.echo(f"  total rows  : {report.total_rows}")
    typer.echo(f"  created     : {report.created_works}")
    typer.echo(f"  added copy  : {report.added_copies}")
    typer.echo(f"  skipped     : {report.skipped_duplicates}")
    if report.enriched_rows:
        typer.echo(f"  enriched    : {report.enriched_rows}")
    typer.echo(f"  errors      : {len(report.errors)}")
    if report.dry_run:
        typer.echo("  (dry-run — no changes persisted)")
    # Per-warning and per-error detail blocks are suppressed under --quiet;
    # the count line `errors: N` above always prints, so cron logs still
    # carry the signal that something needs attention.
    if quiet:
        return
    if report.warnings:
        typer.echo("\nWarnings:")
        for w in report.warnings[:20]:
            typer.echo(f"  - {w}")
        if len(report.warnings) > 20:
            typer.echo(f"  … and {len(report.warnings) - 20} more")
    if report.errors:
        typer.echo("\nErrors:")
        for e in report.errors[:20]:
            typer.echo(f"  row {e.row_number} [{e.identifier}]: {e.message}")
        if len(report.errors) > 20:
            typer.echo(f"  … and {len(report.errors) - 20} more")


def _read_text_input(file: str, *, strict_encoding: bool) -> tuple[io.StringIO, int]:
    """Read a text file (or stdin) as bytes, decode with the project's
    encoding policy, and return a StringIO plus the number of bytes that had
    to be replaced. ``strict_encoding=True`` raises on any decode error."""
    with open_input(file, binary=True) as raw:
        data = raw.read()
    text, replaced = decode_text_bytes(data, strict=strict_encoding)
    return io.StringIO(text), replaced


def _resolve_mode(value: str) -> ImportMode:
    try:
        return ImportMode(value)
    except ValueError as exc:
        valid = ", ".join(m.value for m in ImportMode)
        raise typer.BadParameter(
            f"Unknown mode '{value}'. Valid: {valid}"
        ) from exc


def _common_import_options(
    dry_run: bool,
    mode: str,
    default_branch: str | None,
    default_media_type: str | None,
    enrich: bool = False,
    preserve_barcodes: bool = False,
    strict_encoding: bool = False,
) -> ImportOptions:
    return ImportOptions(
        mode=_resolve_mode(mode),
        dry_run=dry_run,
        default_branch_code=default_branch,
        default_media_type=default_media_type,
        enrich_from_external=enrich,
        preserve_barcodes=preserve_barcodes,
        strict_encoding=strict_encoding,
    )


@import_app.command("csv")
def import_csv_cmd(
    file: str = typer.Argument(..., help="CSV file to import. Use '-' for stdin."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse and validate without writing."),
    mode: str = typer.Option(
        "append",
        "--mode",
        help="Dedup behavior: append | skip-duplicates | error-on-conflict",
    ),
    default_branch: str | None = typer.Option(
        None, "--default-branch", help="Branch code applied to rows without one."
    ),
    default_media_type: str | None = typer.Option(
        None,
        "--default-media-type",
        help="Media type code applied to rows without one.",
    ),
    enrich: bool = typer.Option(
        False,
        "--enrich",
        help=(
            "When a row has an ISBN or UPC, fill missing fields (cover, "
            "description, etc.) from the relevant external source. "
            "Default off — bulk imports of clean data should skip the "
            "per-row HTTP call."
        ),
    ),
    preserve_barcodes: bool = typer.Option(
        False,
        "--preserve-barcodes",
        help=(
            "Preserve barcode and accession_number from the CSV rather than "
            "minting fresh codes. Supplied barcodes must be valid 10/14-digit "
            "Compendium format; non-conformant rows are rejected. "
            "Use for round-tripping a CSV export back into the same catalog."
        ),
    ),
    strict_encoding: bool = typer.Option(
        False,
        "--strict-encoding",
        help=(
            "Reject the file on any non-UTF-8 byte. Default is lenient: "
            "invalid bytes are replaced with U+FFFD and a warning is "
            "added to the report."
        ),
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help=(
            "Suppress per-warning and per-error detail in the import report. "
            "Summary block (counts, file, dry-run note) still prints."
        ),
    ),
) -> None:
    """Import catalog rows from a CSV file."""
    options = _common_import_options(
        dry_run,
        mode,
        default_branch,
        default_media_type,
        enrich,
        preserve_barcodes,
        strict_encoding,
    )
    label = "stdin" if is_stdio(file) else Path(file).name
    try:
        with session_scope() as session:
            importer = _make_importer(session)
            try:
                stream, replaced = _read_text_input(
                    file, strict_encoding=strict_encoding
                )
            except UnicodeDecodeError as exc:
                typer.echo(f"Error: file is not valid UTF-8: {exc}", err=True)
                raise typer.Exit(1) from exc
            report = importer.import_csv(stream, options, filename=label)
            if replaced:
                report.warnings.insert(
                    0,
                    f"Decoded with {replaced} byte replacement(s); "
                    "file is not clean UTF-8.",
                )
            _print_report(report, quiet=quiet)
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if report.errors and not dry_run:
        raise typer.Exit(1 if report.created_works + report.added_copies == 0 else 0)


@import_app.command("librarything")
def import_librarything_cmd(
    file: str = typer.Argument(
        ..., help="LibraryThing TSV export to import. Use '-' for stdin."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse and validate without writing."),
    mode: str = typer.Option(
        "append",
        "--mode",
        help="Dedup behavior: append | skip-duplicates | error-on-conflict",
    ),
    default_branch: str | None = typer.Option(
        None, "--default-branch", help="Branch code applied to rows without one."
    ),
    default_media_type: str | None = typer.Option(
        None,
        "--default-media-type",
        help=(
            "Media type code used when LibraryThing's Media field doesn't map "
            "to a known compendium type (and the row needs a fallback)."
        ),
    ),
    enrich: bool = typer.Option(
        False,
        "--enrich",
        help=(
            "When a row has an ISBN, fill missing fields from the relevant "
            "external source (Open Library for books). Default off."
        ),
    ),
    preserve_barcodes: bool = typer.Option(
        False,
        "--preserve-barcodes",
        help=(
            "Preserve LibraryThing's Barcode column when present. Most LT "
            "exports leave Barcode empty, in which case fresh codes are minted."
        ),
    ),
    strict_encoding: bool = typer.Option(
        False,
        "--strict-encoding",
        help=(
            "Reject the file on any non-UTF-8 byte. Default is lenient: "
            "invalid bytes are replaced with U+FFFD and a warning is added "
            "to the report. Real LibraryThing exports often contain a few "
            "stray non-UTF-8 bytes."
        ),
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help=(
            "Suppress per-warning and per-error detail in the import report. "
            "Summary block (counts, file, dry-run note) still prints."
        ),
    ),
) -> None:
    """Import catalog rows from a LibraryThing TSV export."""
    options = _common_import_options(
        dry_run,
        mode,
        default_branch,
        default_media_type,
        enrich,
        preserve_barcodes,
        strict_encoding,
    )
    label = "stdin" if is_stdio(file) else Path(file).name
    try:
        with session_scope() as session:
            importer = _make_importer(session)
            try:
                stream, replaced = _read_text_input(
                    file, strict_encoding=strict_encoding
                )
            except UnicodeDecodeError as exc:
                typer.echo(f"Error: file is not valid UTF-8: {exc}", err=True)
                raise typer.Exit(1) from exc
            report = importer.import_librarything(stream, options, filename=label)
            if replaced:
                report.warnings.insert(
                    0,
                    f"Decoded with {replaced} byte replacement(s); "
                    "file is not clean UTF-8.",
                )
            _print_report(report, quiet=quiet)
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if report.errors and not dry_run:
        raise typer.Exit(1 if report.created_works + report.added_copies == 0 else 0)


@import_app.command("goodreads")
def import_goodreads_cmd(
    file: str = typer.Argument(
        ..., help="GoodReads library export CSV to import. Use '-' for stdin."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse and validate without writing."),
    mode: str = typer.Option(
        "append",
        "--mode",
        help="Dedup behavior: append | skip-duplicates | error-on-conflict",
    ),
    default_branch: str | None = typer.Option(
        None, "--default-branch", help="Branch code applied to rows without one."
    ),
    default_media_type: str | None = typer.Option(
        None,
        "--default-media-type",
        help=(
            "Media type code used when no type can be inferred from the row "
            "(GoodReads exports are always books, so this is rarely needed)."
        ),
    ),
    enrich: bool = typer.Option(
        False,
        "--enrich",
        help=(
            "When a row has an ISBN, fill missing fields from the relevant "
            "external source (Open Library for books). Default off."
        ),
    ),
    preserve_barcodes: bool = typer.Option(
        False,
        "--preserve-barcodes",
        help=(
            "Preserve barcode values from the import rather than minting fresh "
            "codes. GoodReads exports do not include barcodes, so this has no "
            "effect in practice."
        ),
    ),
    strict_encoding: bool = typer.Option(
        False,
        "--strict-encoding",
        help=(
            "Reject the file on any non-UTF-8 byte. Default is lenient: "
            "invalid bytes are replaced with U+FFFD and a warning is added "
            "to the report."
        ),
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help=(
            "Suppress per-warning and per-error detail in the import report. "
            "Summary block (counts, file, dry-run note) still prints."
        ),
    ),
) -> None:
    """Import catalog rows from a GoodReads library export CSV."""
    options = _common_import_options(
        dry_run,
        mode,
        default_branch,
        default_media_type,
        enrich,
        preserve_barcodes,
        strict_encoding,
    )
    label = "stdin" if is_stdio(file) else Path(file).name
    try:
        with session_scope() as session:
            importer = _make_importer(session)
            try:
                stream, replaced = _read_text_input(
                    file, strict_encoding=strict_encoding
                )
            except UnicodeDecodeError as exc:
                typer.echo(f"Error: file is not valid UTF-8: {exc}", err=True)
                raise typer.Exit(1) from exc
            report = importer.import_goodreads(stream, options, filename=label)
            if replaced:
                report.warnings.insert(
                    0,
                    f"Decoded with {replaced} byte replacement(s); "
                    "file is not clean UTF-8.",
                )
            _print_report(report, quiet=quiet)
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if report.errors and not dry_run:
        raise typer.Exit(1 if report.created_works + report.added_copies == 0 else 0)


@import_app.command("marc")
def import_marc_cmd(
    file: str = typer.Argument(
        ...,
        help="MARC file to import (.mrc binary or .xml). Use '-' for stdin.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    mode: str = typer.Option("append", "--mode"),
    default_branch: str | None = typer.Option(None, "--default-branch"),
    default_media_type: str | None = typer.Option(None, "--default-media-type"),
    xml: bool = typer.Option(
        False,
        "--xml",
        help="Force MARCXML parsing (required when reading XML from stdin).",
    ),
    enrich: bool = typer.Option(
        False,
        "--enrich",
        help=(
            "Fill missing metadata from the external source matching the "
            "record's media type. ISBN/UPC must be present on the record. "
            "Default off."
        ),
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help=(
            "Suppress per-warning and per-error detail in the import report. "
            "Summary block (counts, file, dry-run note) still prints."
        ),
    ),
) -> None:
    """Import catalog records from a MARC21 binary (.mrc) or MARCXML (.xml) file."""
    options = _common_import_options(
        dry_run, mode, default_branch, default_media_type, enrich
    )
    if is_stdio(file):
        is_xml = xml  # extension-sniffing isn't possible for stdin; explicit flag wins
        label = "stdin"
    else:
        is_xml = xml or Path(file).suffix.lower() in {".xml", ".marcxml"}
        label = Path(file).name
    try:
        with session_scope() as session:
            importer = _make_importer(session)
            with open_input(file, binary=True) as stream:
                if is_xml:
                    report = importer.import_marcxml(stream, options, filename=label)
                else:
                    report = importer.import_marc(stream, options, filename=label)
            _print_report(report, quiet=quiet)
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if report.errors and not dry_run:
        raise typer.Exit(1 if report.created_works + report.added_copies == 0 else 0)


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--since must be ISO-8601 (YYYY-MM-DD), got '{value}'"
        ) from exc


def _build_filters(media_type, branch, since) -> ExportFilters:
    return ExportFilters(
        media_type_code=media_type,
        branch_code=branch,
        since=_parse_since(since),
    )


@export_app.command("csv")
def export_csv_cmd(
    output: str = typer.Argument(
        ..., help="Path to write CSV output. Use '-' for stdout."
    ),
    media_type: str | None = typer.Option(
        None, "--media-type", help="Restrict to works of this media_type code."
    ),
    branch: str | None = typer.Option(
        None, "--branch", help="Restrict to items of this branch code."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Restrict to works created on or after this ISO date."
    ),
) -> None:
    """Export catalog to a CSV file."""
    filters = _build_filters(media_type, branch, since)
    to_stdout = is_stdio(output)
    with session_scope() as session:
        exporter = ExportService(work_repo=SqlWorkRepository(session))
        with open_output(output, binary=False) as stream:
            count = exporter.export_csv(stream, filters)
    where = "stdout" if to_stdout else output
    typer.echo(f"Wrote {count} item rows to {where}.", err=to_stdout)


@export_app.command("marc")
def export_marc_cmd(
    output: str = typer.Argument(
        ..., help="Path to write MARC output. Use '-' for stdout."
    ),
    media_type: str | None = typer.Option(None, "--media-type"),
    branch: str | None = typer.Option(None, "--branch"),
    since: str | None = typer.Option(None, "--since"),
    xml: bool = typer.Option(
        False, "--xml", help="Emit MARCXML instead of binary MARC21."
    ),
) -> None:
    """Export catalog to a MARC file (.mrc binary, or MARCXML with --xml)."""
    filters = _build_filters(media_type, branch, since)
    to_stdout = is_stdio(output)
    with session_scope() as session:
        exporter = ExportService(work_repo=SqlWorkRepository(session))
        with open_output(output, binary=True) as stream:
            if xml:
                count = exporter.export_marcxml(stream, filters)
            else:
                count = exporter.export_marc(stream, filters)
    where = "stdout" if to_stdout else output
    typer.echo(f"Wrote {count} records to {where}.", err=to_stdout)
