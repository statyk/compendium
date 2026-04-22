"""MARC21 binary + MARCXML import/export round-trip."""

from __future__ import annotations

import io

import pytest
from pymarc import Field, MARCWriter, Record, Subfield

from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.import_export import (
    ExportFilters,
    ExportService,
    ImportMode,
    ImportOptions,
    ImportService,
)


def _services(session):
    audit = AuditService(SqlAuditLogRepository(session))
    catalog = CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=None,
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


def _mk_book_record(title="Dune", author="Herbert, Frank", isbn="9780441013593"):
    r = Record()
    # leader position 6 = 'a' → book
    leader = list(r.leader)
    while len(leader) < 24:
        leader.append(" ")
    leader[6] = "a"
    leader[7] = "m"
    r.leader = "".join(leader)
    r.add_field(Field(tag="020", indicators=[" ", " "], subfields=[Subfield("a", isbn)]))
    r.add_field(
        Field(tag="100", indicators=["1", " "], subfields=[Subfield("a", author)])
    )
    r.add_field(
        Field(
            tag="245",
            indicators=["1", "0"],
            subfields=[Subfield("a", f"{title} /")],
        )
    )
    r.add_field(
        Field(
            tag="264",
            indicators=[" ", "1"],
            subfields=[Subfield("b", "Chilton Books,"), Subfield("c", "1965.")],
        )
    )
    return r


def _mk_dvd_record(title="Blade Runner", director="Scott, Ridley"):
    r = Record()
    leader = list(r.leader)
    while len(leader) < 24:
        leader.append(" ")
    leader[6] = "g"
    leader[7] = "m"
    r.leader = "".join(leader)
    r.add_field(Field(tag="007", data="vd cvaizq"))
    r.add_field(
        Field(
            tag="100",
            indicators=["1", " "],
            subfields=[Subfield("a", director), Subfield("e", "director")],
        )
    )
    r.add_field(
        Field(
            tag="245",
            indicators=["1", "0"],
            subfields=[Subfield("a", f"{title} /")],
        )
    )
    return r


def _bytes_from(records):
    buf = io.BytesIO()
    w = MARCWriter(buf)
    for rec in records:
        w.write(rec)
    w.close(close_fh=False)
    buf.seek(0)
    return buf


def test_marc_import_creates_book(session):
    importer, _, _ = _services(session)
    stream = _bytes_from([_mk_book_record()])
    report = importer.import_marc(stream, ImportOptions())
    assert report.total_rows == 1
    assert report.created_works == 1
    assert report.errors == []

    w = SqlWorkRepository(session).get_by_isbn("9780441013593")
    assert w is not None
    assert w.title == "Dune"
    assert w.publisher == "Chilton Books"
    assert w.publication_year == 1965
    assert w.media_type.code == "book"
    assert [c.creator.display_name for c in w.creators] == ["Herbert, Frank"]


def test_marc_import_infers_dvd_from_leader_and_007(session):
    importer, _, _ = _services(session)
    stream = _bytes_from([_mk_dvd_record()])
    report = importer.import_marc(stream, ImportOptions())
    assert report.created_works == 1

    works = SqlWorkRepository(session).list()
    br = [w for w in works if w.title == "Blade Runner"][0]
    assert br.media_type.code == "dvd"
    assert br.creators[0].role == "director"


def test_marc_import_unknown_media_type_with_default(session):
    importer, _, _ = _services(session)
    r = Record()
    leader = list(r.leader)
    while len(leader) < 24:
        leader.append(" ")
    leader[6] = "c"  # notated music — unmapped
    r.leader = "".join(leader)
    r.add_field(
        Field(
            tag="245", indicators=["1", "0"], subfields=[Subfield("a", "Sheet Music /")]
        )
    )
    stream = _bytes_from([r])

    report = importer.import_marc(stream, ImportOptions())
    assert report.created_works == 0
    assert len(report.errors) == 1
    assert "media_type" in report.errors[0].message.lower()

    # Re-run with a default specified
    stream2 = _bytes_from([r])
    report2 = importer.import_marc(
        stream2, ImportOptions(default_media_type="book")
    )
    assert report2.created_works == 1


def test_marc_import_missing_title_is_row_error(session):
    importer, _, _ = _services(session)
    r = Record()
    leader = list(r.leader)
    while len(leader) < 24:
        leader.append(" ")
    leader[6] = "a"
    r.leader = "".join(leader)
    r.add_field(
        Field(tag="100", indicators=["1", " "], subfields=[Subfield("a", "Anonymous")])
    )
    stream = _bytes_from([r])
    report = importer.import_marc(stream, ImportOptions())
    assert report.created_works == 0
    assert len(report.errors) == 1
    assert "245" in report.errors[0].message


def test_marc_import_barcode_prefix_applies(session):
    importer, _, _ = _services(session)
    stream = _bytes_from([_mk_book_record()])
    importer.import_marc(stream, ImportOptions(barcode_prefix="IMP-"))
    w = SqlWorkRepository(session).get_by_isbn("9780441013593")
    assert w.items[0].barcode.startswith("IMP-")
    assert not w.items[0].accession_number.startswith("IMP-")


def test_marc_round_trip_preserves_core_fields(session):
    importer, exporter, _ = _services(session)
    stream = _bytes_from([_mk_book_record(), _mk_dvd_record()])
    importer.import_marc(stream, ImportOptions())

    out = io.BytesIO()
    count = exporter.export_marc(out, ExportFilters())
    assert count == 2

    out.seek(0)
    from pymarc import MARCReader

    records = list(MARCReader(out))
    titles = {r["245"].get_subfields("a")[0].rstrip(" /").strip() for r in records}
    assert titles == {"Dune", "Blade Runner"}

    dune = next(r for r in records if r["245"].get_subfields("a")[0].startswith("Dune"))
    assert dune["020"].get_subfields("a")[0] == "9780441013593"
    assert dune.leader[6] == "a"

    br = next(
        r for r in records if r["245"].get_subfields("a")[0].startswith("Blade Runner")
    )
    assert br.leader[6] == "g"
    assert br.get_fields("007")


def test_marc_import_dedups_by_isbn(session):
    importer, _, _ = _services(session)
    stream = _bytes_from([_mk_book_record(), _mk_book_record()])
    report = importer.import_marc(stream, ImportOptions())
    assert report.created_works == 1
    assert report.added_copies == 1


def test_marc_import_skip_duplicates(session):
    importer, _, _ = _services(session)
    importer.import_marc(_bytes_from([_mk_book_record()]), ImportOptions())
    again = importer.import_marc(
        _bytes_from([_mk_book_record()]),
        ImportOptions(mode=ImportMode.SKIP_DUPLICATES),
    )
    assert again.skipped_duplicates == 1


def test_marcxml_round_trip(session):
    importer, exporter, _ = _services(session)
    importer.import_marc(_bytes_from([_mk_book_record()]), ImportOptions())

    xml_buf = io.BytesIO()
    exporter.export_marcxml(xml_buf, ExportFilters())
    xml_buf.seek(0)

    # Fresh session would be ideal, but we can at least verify the bytes parse.
    from pymarc import parse_xml_to_array

    records = parse_xml_to_array(xml_buf)
    assert len(records) == 1
    assert records[0]["245"].get_subfields("a")[0].startswith("Dune")


def test_marc_export_does_not_leak_loanable_state(session):
    importer, exporter, _ = _services(session)
    stream = _bytes_from([_mk_book_record()])
    importer.import_marc(stream, ImportOptions())

    # Flip the imported item non-loanable.
    work = SqlWorkRepository(session).get_by_isbn("9780441013593")
    item = work.items[0]
    item.is_loanable = False
    item.loan_restriction_reason = "reference"
    SqlItemRepository(session).update(item)

    out = io.BytesIO()
    exporter.export_marc(out, ExportFilters())
    out.seek(0)
    raw = out.getvalue()
    # loanable-related strings must not appear in any field.
    assert b"reference" not in raw
    assert b"is_loanable" not in raw
    # Standard fields still present.
    assert b"Herbert" in raw


def test_marc_import_records_audit_entry(session):
    importer, _, audit = _services(session)
    stream = _bytes_from([_mk_book_record()])
    importer.import_marc(stream, ImportOptions(), filename="sample.mrc")
    entries = [
        e for e in audit.list(limit=10) if e.action == "bulk_import"
    ]
    assert len(entries) == 1
    assert entries[0].details["source"] == "marc"
    assert entries[0].details["filename"] == "sample.mrc"
    assert entries[0].details["created_works"] == 1


def test_marcxml_import_malformed_raises_validation_error(session):
    importer, _, _ = _services(session)
    stream = io.BytesIO(b"<not-xml>")
    from compendium.domain.errors import ValidationError

    with pytest.raises(ValidationError):
        importer.import_marcxml(stream, ImportOptions())
