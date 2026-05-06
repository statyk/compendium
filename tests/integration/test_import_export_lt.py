"""LibraryThing TSV import end-to-end + encoding tolerance."""

from __future__ import annotations

import io

import pytest

from compendium.domain.errors import ValidationError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditAction, AuditService
from compendium.services.catalog import CatalogService
from compendium.services.import_export import (
    ImportMode,
    ImportOptions,
    ImportService,
    decode_text_bytes,
)


def _make_importer(session, *, with_audit: bool = True) -> tuple[ImportService, AuditService | None]:
    audit = AuditService(SqlAuditLogRepository(session)) if with_audit else None
    catalog = CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=None,
    )
    return (
        ImportService(
            session=session,
            catalog=catalog,
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            audit_svc=audit,
            source="test",
        ),
        audit,
    )


# Minimal LT-shaped TSV. Real exports have 53 columns; csv.DictReader only
# requires that referenced columns exist. The columns below cover every
# branch the importer touches (mapping, classification, copies, IDs, tags).
_LT_HEADER = (
    "Title\tPrimary Author\tSecondary Author\tPublication\tDate\tMedia\t"
    "Languages\tLC Classification\tDewey Decimal\tISBN\tOther Call Number\t"
    "Copies\tTags\tCollections\tBook Id\tWork id\tOCLC\tBarcode"
)


def _row(**vals: str) -> str:
    fields = [
        "Title",
        "Primary Author",
        "Secondary Author",
        "Publication",
        "Date",
        "Media",
        "Languages",
        "LC Classification",
        "Dewey Decimal",
        "ISBN",
        "Other Call Number",
        "Copies",
        "Tags",
        "Collections",
        "Book Id",
        "Work id",
        "OCLC",
        "Barcode",
    ]
    return "\t".join(vals.get(f, "") for f in fields)


def _tsv(*rows: str) -> str:
    return _LT_HEADER + "\n" + "\n".join(rows) + "\n"


def test_lt_import_creates_works_with_external_ids_and_extra_metadata(session):
    importer, _ = _make_importer(session)
    tsv = _tsv(
        _row(
            Title="Ishmael",
            **{"Primary Author": "Quinn, Daniel"},
            Publication="Bantam (1995), Paperback, 263 pages",
            Date="1995",
            Media="Paperback",
            Languages="English",
            **{"LC Classification": "PS3567 .U338"},
            ISBN="[0553375407]",
            Copies="1",
            Tags="Fiction, Philosophy",
            **{"Book Id": "12345", "OCLC": "67890"},
        ),
    )
    report = importer.import_librarything(io.StringIO(tsv), ImportOptions())
    assert report.errors == []
    assert report.created_works == 1
    assert report.added_copies == 0

    # ISBN-10 normalizes to ISBN-13 inside _process_csv_row.
    work = SqlWorkRepository(session).get_by_isbn("9780553375404")
    assert work is not None
    assert work.title == "Ishmael"
    assert work.publisher == "Bantam"
    assert work.publication_year == 1995
    assert work.language == "en"
    assert work.classification_scheme == "LCC"
    assert work.classification_code == "PS3567 .U338"
    assert work.media_type.code == "book"
    assert work.external_ids == {
        "librarything": {"book_id": "12345", "oclc": "67890"}
    }
    assert work.extra_metadata == {
        "librarything": {"tags": ["Fiction", "Philosophy"]}
    }
    assert len(work.items) == 1


def test_lt_import_copies_greater_than_one_creates_multiple_items(session):
    importer, _ = _make_importer(session)
    tsv = _tsv(
        _row(
            Title="Foundation",
            **{"Primary Author": "Asimov, Isaac"},
            Date="1951",
            Media="Hardcover",
            Languages="English",
            ISBN="[9780553293357]",
            Copies="3",
        ),
    )
    report = importer.import_librarything(io.StringIO(tsv), ImportOptions())
    assert report.errors == []
    assert report.created_works == 1
    assert report.added_copies == 2  # copies 2 and 3 land as added_copy

    work = SqlWorkRepository(session).get_by_isbn("9780553293357")
    assert len(work.items) == 3
    barcodes = {item.barcode for item in work.items}
    assert len(barcodes) == 3  # all distinct (minted)


def test_lt_import_copies_loop_forces_append_with_warning_under_skip_mode(session):
    importer, _ = _make_importer(session)
    tsv = _tsv(
        _row(
            Title="Dune",
            **{"Primary Author": "Herbert, Frank"},
            Date="1965",
            Media="Paperback",
            ISBN="[9780441013593]",
            Copies="2",
        ),
    )
    report = importer.import_librarything(
        io.StringIO(tsv),
        ImportOptions(mode=ImportMode.SKIP_DUPLICATES),
    )
    work = SqlWorkRepository(session).get_by_isbn("9780441013593")
    # Without the override, copy 2 would be skipped under skip-duplicates.
    # With the override, copies 2..N go through as added_copy.
    assert len(work.items) == 2
    assert any("forced append" in w for w in report.warnings)


def test_lt_import_dry_run_does_not_persist(session):
    importer, _ = _make_importer(session)
    tsv = _tsv(
        _row(
            Title="Foundation",
            **{"Primary Author": "Asimov, Isaac"},
            Media="Paperback",
            ISBN="[9780553293357]",
        ),
    )
    report = importer.import_librarything(
        io.StringIO(tsv), ImportOptions(dry_run=True)
    )
    assert report.created_works == 1
    assert report.dry_run is True
    assert SqlWorkRepository(session).list() == []


def test_lt_import_records_bulk_audit_entry(session):
    importer, audit = _make_importer(session)
    tsv = _tsv(
        _row(Title="Solo", **{"Primary Author": "Solo, Han"}, Media="Paperback"),
    )
    importer.import_librarything(
        io.StringIO(tsv), ImportOptions(), filename="lt_export.tsv"
    )
    entries = audit.list(entity_type="work", limit=10)
    bulk = [e for e in entries if e.action == AuditAction.BULK_IMPORT]
    assert len(bulk) == 1
    assert bulk[0].details["source"] == "librarything"
    assert bulk[0].details["filename"] == "lt_export.tsv"
    assert bulk[0].details["total_rows"] == 1
    assert bulk[0].details["created_works"] == 1


def test_lt_import_missing_required_header_raises(session):
    importer, _ = _make_importer(session)
    bad_tsv = "Author\tISBN\nFoo\t1234\n"
    with pytest.raises(ValidationError, match="missing required column 'Title'"):
        importer.import_librarything(io.StringIO(bad_tsv), ImportOptions())


def test_lt_import_through_lenient_decode_records_warning(session):
    importer, _ = _make_importer(session)
    # Build the TSV bytes with a stray cp1252 'è' byte inside one Title.
    raw = _tsv(
        _row(
            Title="Lun YAYU",
            **{"Primary Author": "Confucius"},
            Media="Hardcover",
            ISBN="[9781234567897]",
        ),
    ).encode("utf-8").replace(b"YAYU", b"Y\xe8u")
    text, replaced = decode_text_bytes(raw, strict=False)
    assert replaced == 1
    report = importer.import_librarything(io.StringIO(text), ImportOptions())
    assert report.errors == []
    assert report.created_works == 1
    work = SqlWorkRepository(session).get_by_isbn("9781234567897")
    assert "Lun Y" in work.title
    assert "�" in work.title


def test_lt_decode_strict_rejects_stray_byte():
    raw = b"Title\nLun Y\xe8u\n"
    with pytest.raises(UnicodeDecodeError):
        decode_text_bytes(raw, strict=True)
