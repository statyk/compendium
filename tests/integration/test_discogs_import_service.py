"""Discogs collection-CSV import end-to-end.

Mirrors the LibraryThing/GoodReads service tests: build the CSV text inline
(the module convention — see ``test_import_export_lt.py``), open a fresh
``io.StringIO`` per import, and drive ``ImportService.import_discogs`` against a
seeded SQLite session. No on-disk fixture file: the existing importer tests keep
their sample data inline, so this one follows suit.
"""

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
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.import_export import (
    ImportMode,
    ImportOptions,
    ImportService,
)


def _make_importer(session) -> ImportService:
    audit = AuditService(SqlAuditLogRepository(session))
    catalog = CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=None,
    )
    return ImportService(
        session=session,
        catalog=catalog,
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        audit_svc=audit,
        source="test",
    )


# Discogs collection-export shape. Two vinyl rows + one cassette row (the
# cassette Format has no vinyl/cd descriptor → expected per-row error).
_HEADER = (
    "Catalog#,Artist,Title,Label,Format,Rating,Released,release_id,"
    "CollectionFolder,Date Added,Collection Media Condition,"
    "Collection Sleeve Condition,Collection Notes"
)
_MILES = (
    'CL 1355,Miles Davis,Kind of Blue,Columbia,"Vinyl, LP, Album, Reissue",5,'
    "1959,12345,Jazz,2026-01-02 10:00:00,Near Mint (NM or M-),"
    "Very Good Plus (VG+),first pressing"
)
_NIRVANA = (
    'DGC-24425,Nirvana (2),Nevermind,DGC,"Vinyl, LP, Album",4,1991,67890,Rock,'
    "2026-01-03 11:00:00,Very Good Plus (VG+),Very Good (VG),"
)
_CASSETTE = (
    ',Various,Some Mixtape,,"Cassette, Album",,1994,55555,Tapes,'
    "2026-01-04 09:00:00,,,"
)


def _full_csv() -> io.StringIO:
    return io.StringIO("\n".join([_HEADER, _MILES, _NIRVANA, _CASSETTE]) + "\n")


def _vinyl_only_csv() -> io.StringIO:
    return io.StringIO("\n".join([_HEADER, _MILES, _NIRVANA]) + "\n")


def test_import_discogs_creates_works(session):
    importer = _make_importer(session)
    report = importer.import_discogs(_full_csv(), ImportOptions())

    assert report.source == "discogs"
    assert report.total_rows == 3
    assert report.created_works == 2  # cassette row errors
    assert report.added_copies == 0
    assert len(report.errors) == 1
    # The cassette row is the one that failed (row 4 in the CSV).
    assert report.errors[0].identifier == "Some Mixtape"

    works = SqlWorkRepository(session)

    miles = works.get_by_external_id("discogs", "12345")
    assert miles is not None
    assert miles.title == "Kind of Blue"
    assert miles.publisher == "Columbia"
    assert miles.publication_year == 1959
    assert miles.media_type.code == "vinyl"
    assert miles.external_ids == {"discogs": "12345"}
    assert miles.creators[0].creator.display_name == "Miles Davis"
    assert miles.creators[0].role == "artist"
    # One owned copy per Discogs row — notes/condition/location land on it.
    assert len(miles.items) == 1
    item = miles.items[0]
    assert item.notes == "first pressing"
    assert item.condition == "NM/VG+"  # media/sleeve grades joined
    assert item.location == "Jazz"

    # Disambiguation suffix "(2)" is stripped from the artist name.
    nirvana = works.get_by_external_id("discogs", "67890")
    assert nirvana is not None
    assert nirvana.creators[0].creator.display_name == "Nirvana"
    assert nirvana.external_ids == {"discogs": "67890"}

    # Cassette row created nothing.
    assert works.get_by_external_id("discogs", "55555") is None


def test_import_discogs_dry_run_writes_nothing(session):
    importer = _make_importer(session)
    report = importer.import_discogs(_full_csv(), ImportOptions(dry_run=True))

    assert report.dry_run is True
    assert report.created_works == 2
    assert SqlWorkRepository(session).list() == []


def test_import_discogs_duplicate_release_id_skip(session):
    importer = _make_importer(session)
    importer.import_discogs(_vinyl_only_csv(), ImportOptions())

    report = importer.import_discogs(
        _vinyl_only_csv(), ImportOptions(mode=ImportMode.SKIP_DUPLICATES)
    )
    assert report.skipped_duplicates == 2
    assert report.created_works == 0
    assert report.added_copies == 0


def test_import_discogs_duplicate_release_id_append(session):
    importer = _make_importer(session)
    importer.import_discogs(_vinyl_only_csv(), ImportOptions())

    report = importer.import_discogs(
        _vinyl_only_csv(), ImportOptions(mode=ImportMode.APPEND)
    )
    assert report.added_copies == 2
    assert report.created_works == 0

    # The APPEND landed second copies on the existing works, not new works.
    miles = SqlWorkRepository(session).get_by_external_id("discogs", "12345")
    assert len(miles.items) == 2


def test_import_discogs_duplicate_release_id_error(session):
    importer = _make_importer(session)
    importer.import_discogs(_vinyl_only_csv(), ImportOptions())

    report = importer.import_discogs(
        _vinyl_only_csv(), ImportOptions(mode=ImportMode.ERROR_ON_CONFLICT)
    )
    assert len(report.errors) == 2
    assert report.created_works == 0
    assert all("error-on-conflict" in e.message for e in report.errors)


def test_import_discogs_missing_header_raises(session):
    importer = _make_importer(session)
    with pytest.raises(ValidationError, match="release_id"):
        importer.import_discogs(io.StringIO("Title\nX\n"), ImportOptions())
