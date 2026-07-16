"""CSV import + export round-trip and error paths."""

from __future__ import annotations

import csv
import io

import pytest

from compendium.domain.enums import LoanRestrictionReason
from compendium.domain.errors import ValidationError
from compendium.domain.identifiers import ITEM_TYPE, validate_barcode
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditAction, AuditService
from compendium.services.catalog import CatalogService
from compendium.services.import_export import (
    ExportFilters,
    ExportService,
    ImportMode,
    ImportOptions,
    ImportService,
)


def _make_services(session, *, with_audit: bool = True):
    audit = AuditService(SqlAuditLogRepository(session)) if with_audit else None
    catalog = CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=None,  # importer does not emit per-row audits
    )
    importer = ImportService(
        session=session,
        catalog=catalog,
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        audit_svc=audit,
        source="test",
    )
    exporter = ExportService(work_repo=SqlWorkRepository(session))
    return importer, exporter, audit


_MINIMAL_CSV = """media_type,title,authors,isbn
book,Dune,Frank Herbert,9780441013593
book,Foundation,Isaac Asimov,9780553293357
"""


def test_csv_import_creates_works_and_items(session):
    importer, _, _ = _make_services(session)
    report = importer.import_csv(io.StringIO(_MINIMAL_CSV), ImportOptions())
    assert report.total_rows == 2
    assert report.created_works == 2
    assert report.added_copies == 0
    assert report.errors == []

    works = SqlWorkRepository(session).list()
    titles = {w.title for w in works}
    assert "Dune" in titles
    assert "Foundation" in titles


def test_csv_import_dedups_by_isbn_append_mode(session):
    importer, _, _ = _make_services(session)
    csv_text = """media_type,title,authors,isbn
book,Dune,Frank Herbert,9780441013593
book,Dune,Frank Herbert,9780441013593
"""
    report = importer.import_csv(io.StringIO(csv_text), ImportOptions())
    assert report.created_works == 1
    assert report.added_copies == 1

    dune = SqlWorkRepository(session).get_by_isbn("9780441013593")
    assert dune is not None
    assert len(dune.items) == 2


def test_csv_import_skip_duplicates(session):
    importer, _, _ = _make_services(session)
    importer.import_csv(io.StringIO(_MINIMAL_CSV), ImportOptions())
    again = importer.import_csv(
        io.StringIO(_MINIMAL_CSV),
        ImportOptions(mode=ImportMode.SKIP_DUPLICATES),
    )
    assert again.created_works == 0
    assert again.added_copies == 0
    assert again.skipped_duplicates == 2


def test_csv_import_error_on_conflict(session):
    importer, _, _ = _make_services(session)
    importer.import_csv(io.StringIO(_MINIMAL_CSV), ImportOptions())
    again = importer.import_csv(
        io.StringIO(_MINIMAL_CSV),
        ImportOptions(mode=ImportMode.ERROR_ON_CONFLICT),
    )
    assert again.created_works == 0
    assert len(again.errors) == 2
    assert "Duplicate ISBN/UPC" in again.errors[0].message


def test_csv_import_missing_title_is_row_error(session):
    importer, _, _ = _make_services(session)
    csv_text = """media_type,title,authors,isbn
book,,Frank Herbert,9780441013593
book,Foundation,Isaac Asimov,9780553293357
"""
    report = importer.import_csv(io.StringIO(csv_text), ImportOptions())
    assert report.created_works == 1
    assert len(report.errors) == 1
    assert report.errors[0].row_number == 2
    assert "title" in report.errors[0].message.lower()


def test_csv_import_unknown_media_type_is_row_error(session):
    importer, _, _ = _make_services(session)
    csv_text = """media_type,title,isbn
bogus,Strange Book,9780441013593
"""
    report = importer.import_csv(io.StringIO(csv_text), ImportOptions())
    assert report.created_works == 0
    assert len(report.errors) == 1
    assert "media_type" in report.errors[0].message.lower()


def test_csv_import_missing_required_header(session):
    importer, _, _ = _make_services(session)
    csv_text = """media_type,author,isbn
book,Frank Herbert,9780441013593
"""
    with pytest.raises(ValidationError, match="missing required columns"):
        importer.import_csv(io.StringIO(csv_text), ImportOptions())


def test_csv_import_dry_run_does_not_persist(session):
    importer, _, _ = _make_services(session)
    report = importer.import_csv(io.StringIO(_MINIMAL_CSV), ImportOptions(dry_run=True))
    assert report.created_works == 2
    assert report.dry_run is True
    assert SqlWorkRepository(session).list() == []


def test_csv_import_discards_explicit_barcode_by_default(session):
    """Default mode discards supplied non-conformant barcodes and mints fresh codes."""
    importer, _, _ = _make_services(session)
    csv_text = """media_type,title,authors,isbn,barcode
book,Dune,Frank Herbert,9780441013593,LIB-00001
"""
    importer.import_csv(io.StringIO(csv_text), ImportOptions())
    dune = SqlWorkRepository(session).get_by_isbn("9780441013593")
    barcode = dune.items[0].barcode
    assert barcode != "LIB-00001"
    assert validate_barcode(barcode, expected_type=ITEM_TYPE) is not None


def test_csv_import_loanable_fields(session):
    importer, _, _ = _make_services(session)
    csv_text = """media_type,title,authors,isbn,is_loanable,loan_restriction_reason,loan_restriction_note
book,Ref Manual,Jane Doe,9780000000001,no,reference,
book,Donor Copy,Jane Doe,9780000000002,no,other,per donor agreement
"""
    report = importer.import_csv(io.StringIO(csv_text), ImportOptions())
    assert report.created_works == 2
    ref = SqlWorkRepository(session).get_by_isbn("9780000000001")
    donor = SqlWorkRepository(session).get_by_isbn("9780000000002")
    assert ref.items[0].is_loanable is False
    assert ref.items[0].loan_restriction_reason == LoanRestrictionReason.REFERENCE.value
    assert donor.items[0].loan_restriction_note == "per donor agreement"


def test_csv_import_loanable_other_requires_note(session):
    importer, _, _ = _make_services(session)
    csv_text = """media_type,title,authors,isbn,is_loanable,loan_restriction_reason,loan_restriction_note
book,Donor,Jane Doe,9780000000003,no,other,
"""
    report = importer.import_csv(io.StringIO(csv_text), ImportOptions())
    assert report.created_works == 0
    assert len(report.errors) == 1
    assert "note" in report.errors[0].message.lower()


def test_csv_import_records_bulk_audit_entry(session):
    importer, _, audit = _make_services(session)
    importer.import_csv(io.StringIO(_MINIMAL_CSV), ImportOptions(), filename="books.csv")
    entries = audit.list(entity_type="work", limit=10)
    bulk = [e for e in entries if e.action == AuditAction.BULK_IMPORT]
    assert len(bulk) == 1
    assert bulk[0].details["source"] == "csv"
    assert bulk[0].details["filename"] == "books.csv"
    assert bulk[0].details["total_rows"] == 2
    assert bulk[0].details["created_works"] == 2


def test_csv_import_dry_run_does_not_record_audit(session):
    importer, _, audit = _make_services(session)
    importer.import_csv(io.StringIO(_MINIMAL_CSV), ImportOptions(dry_run=True))
    bulk = [e for e in audit.list(limit=10) if e.action == AuditAction.BULK_IMPORT]
    assert bulk == []


def test_csv_round_trip(session):
    importer, exporter, _ = _make_services(session)
    csv_text = """media_type,title,subtitle,authors,publisher,publication_year,isbn,upc,classification_scheme,classification_code,description,language,barcode,accession_number,branch,call_number,condition,location,is_loanable,loan_restriction_reason,loan_restriction_note
book,Dune,Frank Herbert's classic,Frank Herbert,Chilton Books,1965,9780441013593,,lcc,PS3558.E63 D8,A space opera,en,DUNE-001,000001,MAIN,SF HER,good,Shelf A,yes,,
dvd,Blade Runner,The Final Cut,Ridley Scott:director,Warner Bros,2007,,012569810426,,,,en,BR-0001,000002,MAIN,,,Film Shelf,yes,,
"""
    importer.import_csv(io.StringIO(csv_text), ImportOptions())

    out = io.StringIO()
    count = exporter.export_csv(out, ExportFilters())
    assert count == 2

    out.seek(0)
    rows = list(csv.DictReader(out))
    dune = next(r for r in rows if r["title"] == "Dune")
    assert dune["isbn"] == "9780441013593"
    assert dune["classification_code"] == "PS3558.E63 D8"
    # Supplied legacy barcode was discarded; exported barcode is a fresh conformant code.
    assert validate_barcode(dune["barcode"], expected_type=ITEM_TYPE) is not None
    assert dune["authors"] == "Frank Herbert:author"

    bld = next(r for r in rows if r["title"] == "Blade Runner")
    assert bld["upc"] == "012569810426"
    assert bld["authors"] == "Ridley Scott:director"


def test_csv_notes_round_trips(session):
    """Per-copy notes survive both export and reimport."""
    importer, exporter, _ = _make_services(session)
    csv_text = """media_type,title,authors,isbn,notes
book,Dune,Frank Herbert,9780441013593,"shelf B, sleeve torn"
"""
    importer.import_csv(io.StringIO(csv_text), ImportOptions())

    dune = SqlWorkRepository(session).get_by_isbn("9780441013593")
    assert dune.items[0].notes == "shelf B, sleeve torn"

    # Export emits the notes column with the value.
    out = io.StringIO()
    exporter.export_csv(out, ExportFilters())
    out.seek(0)
    rows = list(csv.DictReader(out))
    exported = next(r for r in rows if r["title"] == "Dune")
    assert exported["notes"] == "shelf B, sleeve torn"

    # Reimport the exported CSV (append mode mints a fresh barcode) preserves notes.
    out.seek(0)
    report = importer.import_csv(out, ImportOptions())
    assert report.added_copies == 1
    session.expire_all()
    dune = SqlWorkRepository(session).get_by_isbn("9780441013593")
    assert len(dune.items) == 2
    assert all(item.notes == "shelf B, sleeve torn" for item in dune.items)
