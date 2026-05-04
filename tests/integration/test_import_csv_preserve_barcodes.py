"""Tests for ImportOptions.preserve_barcodes mode."""

from __future__ import annotations

import io

from compendium.domain.identifiers import ITEM_TYPE, format_item_barcode, validate_barcode
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.repositories.sql.counters import SqlCounterRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.import_export import ExportFilters, ExportService, ImportOptions, ImportService


def _make_services(session):
    catalog = CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        counter_repo=SqlCounterRepository(session),
    )
    importer = ImportService(
        session=session,
        catalog=catalog,
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        source="test",
    )
    exporter = ExportService(work_repo=SqlWorkRepository(session))
    return importer, exporter


def _valid_barcode(n: int) -> str:
    return format_item_barcode(f"{n:08d}", location_code=None)


# --- preserve_barcodes=False (default) ----------------------------------------

def test_default_mode_discards_supplied_barcode(session):
    """When preserve_barcodes is False, supplied barcodes are discarded and
    fresh conformant codes are minted."""
    csv_text = f"media_type,title,authors,isbn,barcode\nbook,Dune,Frank Herbert,9780441013593,LEGACY-001\n"
    importer, _ = _make_services(session)
    report = importer.import_csv(io.StringIO(csv_text), ImportOptions())
    assert report.errors == []
    work = SqlWorkRepository(session).get_by_isbn("9780441013593")
    barcode = work.items[0].barcode
    # Fresh barcode minted — not the legacy one from the CSV.
    assert barcode != "LEGACY-001"
    assert validate_barcode(barcode, expected_type=ITEM_TYPE) is not None


def test_default_mode_discards_supplied_accession(session):
    """Supplied accession_number is also discarded in default mode."""
    csv_text = (
        "media_type,title,authors,isbn,barcode,accession_number\n"
        "book,Dune,Frank Herbert,9780441013593,,OLD-ACC\n"
    )
    importer, _ = _make_services(session)
    report = importer.import_csv(io.StringIO(csv_text), ImportOptions())
    assert report.errors == []
    work = SqlWorkRepository(session).get_by_isbn("9780441013593")
    assert len(work.items[0].accession_number) == 8


# --- preserve_barcodes=True ---------------------------------------------------

def test_preserve_mode_keeps_valid_barcode(session):
    """When preserve_barcodes is True, a valid Compendium barcode is stored."""
    good_barcode = _valid_barcode(42)
    csv_text = (
        f"media_type,title,authors,isbn,barcode\n"
        f"book,Dune,Frank Herbert,9780441013593,{good_barcode}\n"
    )
    importer, _ = _make_services(session)
    report = importer.import_csv(
        io.StringIO(csv_text), ImportOptions(preserve_barcodes=True)
    )
    assert report.errors == []
    work = SqlWorkRepository(session).get_by_isbn("9780441013593")
    assert work.items[0].barcode == good_barcode


def test_preserve_mode_rejects_non_conformant_barcode(session):
    """A non-conformant barcode causes the row to be rejected with a clear error."""
    csv_text = (
        "media_type,title,authors,isbn,barcode\n"
        "book,Dune,Frank Herbert,9780441013593,LEGACY-001\n"
    )
    importer, _ = _make_services(session)
    report = importer.import_csv(
        io.StringIO(csv_text), ImportOptions(preserve_barcodes=True)
    )
    assert len(report.errors) == 1
    assert "LEGACY-001" in report.errors[0].message
    # Row was rejected; no work or item created.
    assert SqlWorkRepository(session).get_by_isbn("9780441013593") is None


def test_preserve_mode_rejects_bad_luhn(session):
    """A barcode with the right length but wrong check digit is rejected."""
    good = _valid_barcode(1)
    # Flip the last digit to produce a wrong check digit.
    bad_check = str((int(good[-1]) + 1) % 10)
    mangled = good[:-1] + bad_check
    csv_text = (
        f"media_type,title,authors,isbn,barcode\n"
        f"book,Dune,Frank Herbert,9780441013593,{mangled}\n"
    )
    importer, _ = _make_services(session)
    report = importer.import_csv(
        io.StringIO(csv_text), ImportOptions(preserve_barcodes=True)
    )
    assert len(report.errors) == 1


def test_preserve_mode_rejects_patron_type_barcode(session):
    """A patron-type barcode (starting with 2) is rejected for an item row."""
    from compendium.domain.identifiers import format_patron_card
    patron_barcode = format_patron_card("00000099", location_code=None)
    csv_text = (
        f"media_type,title,authors,isbn,barcode\n"
        f"book,Dune,Frank Herbert,9780441013593,{patron_barcode}\n"
    )
    importer, _ = _make_services(session)
    report = importer.import_csv(
        io.StringIO(csv_text), ImportOptions(preserve_barcodes=True)
    )
    assert len(report.errors) == 1


def test_preserve_mode_csv_round_trip(session):
    """Barcodes exported as valid Compendium codes round-trip correctly."""
    # Import two works to get auto-minted barcodes.
    csv_text = (
        "media_type,title,authors,isbn\n"
        "book,Dune,Frank Herbert,9780441013593\n"
        "book,Foundation,Isaac Asimov,9780553293357\n"
    )
    importer, exporter = _make_services(session)
    report1 = importer.import_csv(io.StringIO(csv_text), ImportOptions())
    assert report1.errors == []

    # Export to CSV.
    out = io.StringIO()
    count = exporter.export_csv(out, ExportFilters())
    assert count == 2
    exported_csv = out.getvalue()

    # All exported barcodes should be valid Compendium format.
    import csv as _csv
    rows = list(_csv.DictReader(io.StringIO(exported_csv)))
    for row in rows:
        assert validate_barcode(row["barcode"], expected_type=ITEM_TYPE) is not None

    # Re-importing the same CSV with preserve_barcodes=True should reject rows
    # where the barcode already exists (they'd collide with the existing items).
    report2 = importer.import_csv(
        io.StringIO(exported_csv), ImportOptions(preserve_barcodes=True)
    )
    # Rows are rejected due to duplicate barcodes (not format errors).
    assert report2.created_works == 0
    assert all("already exists" in e.message for e in report2.errors)
