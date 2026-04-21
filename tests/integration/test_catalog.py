from unittest.mock import patch

import pytest

from compendium.domain.errors import ExternalLookupError, NotFoundError, ValidationError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditEntityType, AuditService
from compendium.services.catalog import CatalogService

_OPEN_LIB_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {"openlibrary": ["OL7353617M"]},
}

_ISBN = "9780441013593"


def _service(session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
    )


@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_add_from_isbn_creates_work_and_item(_, session):
    work, item = _service(session).add_from_isbn(_ISBN)

    assert work.title == "Dune"
    assert work.isbn == _ISBN
    assert work.publisher == "Chilton Books"
    assert work.publication_year == 1965
    assert len(work.creators) == 1
    assert work.creators[0].creator.display_name == "Frank Herbert"

    assert item.barcode == "000001"
    assert item.accession_number == "000001"
    assert item.status == "available"
    assert item.work_id == work.id


@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_add_same_isbn_twice_reuses_work(_, session):
    work1, item1 = _service(session).add_from_isbn(_ISBN)
    work2, item2 = _service(session).add_from_isbn(_ISBN)

    assert work1.id == work2.id
    assert item1.id != item2.id
    assert item2.barcode == "000002"


@patch("compendium.services.metadata.lookup_isbn", return_value={})
def test_add_from_isbn_raises_when_not_found(_, session):
    with pytest.raises(ExternalLookupError):
        _service(session).add_from_isbn(_ISBN)


_OPEN_LIB_DUPLICATE_AUTHOR = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}, {"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}


@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUPLICATE_AUTHOR)
def test_add_from_isbn_dedupes_duplicate_authors(_, session):
    """Regression: some Open Library records list the same author twice, which
    used to raise a UNIQUE-constraint error on work_creator."""
    work, _ = _service(session).add_from_isbn(_ISBN)
    assert len(work.creators) == 1
    assert work.creators[0].creator.display_name == "Frank Herbert"


@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_add_item_to_work_adds_copy(_, session):
    work, item1 = _service(session).add_from_isbn(_ISBN)
    item2 = _service(session).add_item_to_work(work.id)

    assert item2.work_id == work.id
    assert item2.id != item1.id


# ── MusicBrainz / vinyl / CD ──────────────────────────────────────────────────

_UPC = "724353063870"

_MB_VINYL_META = {
    "title": "Kind of Blue",
    "subtitle": None,
    "authors": ["Miles Davis"],
    "creator_role": "artist",
    "publisher": "Columbia",
    "publication_year": 1959,
    "description": None,
    "cover_image_url": None,
    "isbn": None,
    "upc": _UPC,
    "external_ids": {"musicbrainz": "cb7b5c31-10ef-4b73-a42f-80d9af8b6aee"},
    "extra_metadata": {
        "format": "Vinyl",
        "tracks": [
            {"position": 1, "title": "So What", "length_ms": 562000},
            {"position": 2, "title": "Freddie Freeloader", "length_ms": 583000},
        ],
        "track_count": 2,
    },
}


@patch("compendium.services.metadata._mb_lookup_by_upc", return_value=_MB_VINYL_META)
def test_add_vinyl_by_upc_creates_work_and_item(_, session):
    work, item = _service(session).add_from_lookup("vinyl", "upc", _UPC)

    assert work.title == "Kind of Blue"
    assert work.upc == _UPC
    assert work.isbn is None
    assert work.publication_year == 1959
    assert len(work.creators) == 1
    assert work.creators[0].creator.display_name == "Miles Davis"
    assert work.creators[0].role == "artist"
    assert work.extra_metadata["track_count"] == 2
    assert item.status == "available"


@patch("compendium.services.metadata._mb_lookup_by_upc", return_value=_MB_VINYL_META)
def test_add_same_upc_twice_reuses_work(_, session):
    work1, item1 = _service(session).add_from_lookup("vinyl", "upc", _UPC)
    work2, item2 = _service(session).add_from_lookup("vinyl", "upc", _UPC)

    assert work1.id == work2.id
    assert item1.id != item2.id


@patch("compendium.services.metadata._mb_lookup_by_upc", return_value=None)
def test_add_from_lookup_raises_when_not_found(_, session):
    with pytest.raises(ExternalLookupError):
        _service(session).add_from_lookup("vinyl", "upc", _UPC)


# ── TMDb / DVD / Blu-ray / VHS ────────────────────────────────────────────────

_TMDB_ID = "497"

_TMDB_MOVIE_META = {
    "title": "The Green Mile",
    "subtitle": None,
    "authors": ["Frank Darabont"],
    "creator_role": "director",
    "creators": [("Frank Darabont", "director")],
    "publisher": None,
    "publication_year": 1999,
    "description": "A supernatural tale set on death row.",
    "cover_image_url": None,
    "isbn": None,
    "upc": None,
    "external_ids": {"tmdb": "497", "imdb": "tt0120689"},
    "extra_metadata": {
        "runtime_minutes": 189,
        "genres": ["Drama", "Fantasy"],
        "original_language": "en",
        "tagline": "Miracles do happen.",
        "release_date": "1999-12-10",
        "cast": ["Tom Hanks", "Michael Clarke Duncan"],
    },
}


@patch("compendium.services.metadata._tmdb_fetch_movie", return_value={
    "id": 497, "title": "The Green Mile", "release_date": "1999-12-10",
    "overview": "A supernatural tale.", "runtime": 189, "tagline": "Miracles do happen.",
    "original_language": "en", "poster_path": None, "imdb_id": "tt0120689",
    "genres": [{"id": 18, "name": "Drama"}],
    "credits": {
        "crew": [{"name": "Frank Darabont", "job": "Director", "department": "Directing"}],
        "cast": [{"name": "Tom Hanks", "order": 0}],
    },
})
def test_add_dvd_by_tmdb_id_creates_work_and_item(mock_fetch, session):
    import os
    with patch.dict(os.environ, {"COMPENDIUM_TMDB_API_KEY": "testkey"}):
        work, item = _service(session).add_from_lookup("dvd", "tmdb_id", _TMDB_ID)

    assert work.title == "The Green Mile"
    assert work.isbn is None
    assert work.upc is None
    assert work.publication_year == 1999
    assert work.external_ids["tmdb"] == "497"
    assert work.extra_metadata["runtime_minutes"] == 189
    assert "Drama" in work.extra_metadata["genres"]
    assert item.status == "available"


@patch("compendium.services.metadata._tmdb_fetch_movie", return_value={
    "id": 497, "title": "The Green Mile", "release_date": "1999-12-10",
    "overview": "A supernatural tale.", "runtime": 189, "tagline": None,
    "original_language": "en", "poster_path": None, "imdb_id": "tt0120689",
    "genres": [],
    "credits": {
        "crew": [
            {"name": "Frank Darabont", "job": "Director", "department": "Directing"},
            {"name": "Frank Darabont", "job": "Screenplay", "department": "Writing"},
        ],
        "cast": [],
    },
})
def test_add_dvd_director_and_writer_as_creators(mock_fetch, session):
    import os
    with patch.dict(os.environ, {"COMPENDIUM_TMDB_API_KEY": "testkey"}):
        work, _ = _service(session).add_from_lookup("dvd", "tmdb_id", _TMDB_ID)

    roles = {wc.role for wc in work.creators}
    names = {wc.creator.display_name for wc in work.creators}
    assert "Frank Darabont" in names
    assert "director" in roles
    assert "writer" in roles


@patch("compendium.services.metadata._tmdb_fetch_movie", return_value={"success": False})
def test_add_dvd_raises_when_not_found(mock_fetch, session):
    import os
    with patch.dict(os.environ, {"COMPENDIUM_TMDB_API_KEY": "testkey"}):
        with pytest.raises(ExternalLookupError):
            _service(session).add_from_lookup("dvd", "tmdb_id", "99999")


# ── Manual entry ──────────────────────────────────────────────────────────────

def test_add_manual_book(session):
    work, item = _service(session).add_manual(
        "book",
        "A Very Obscure Zine",
        authors=["Jane Doe", "John Roe"],
        publisher="Self-published",
        publication_year=2018,
        isbn="9780000000002",
        description="Not on Open Library.",
        location="Shelf Z",
    )
    assert work.title == "A Very Obscure Zine"
    assert work.isbn == "9780000000002"
    assert work.publisher == "Self-published"
    assert work.publication_year == 2018
    assert {wc.creator.display_name for wc in work.creators} == {"Jane Doe", "John Roe"}
    assert all(wc.role == "author" for wc in work.creators)
    assert item.status == "available"
    assert item.location == "Shelf Z"


def test_add_manual_vinyl_uses_artist_role(session):
    work, _ = _service(session).add_manual(
        "vinyl", "Basement Tapes", authors=["Obscure Band"]
    )
    assert work.creators[0].role == "artist"


def test_add_manual_dedupes_by_isbn(session):
    svc = _service(session)
    work1, item1 = svc.add_manual("book", "Widgets", isbn="9780000000019")
    work2, item2 = svc.add_manual("book", "Different Title", isbn="9780000000019")
    assert work1.id == work2.id
    assert item1.id != item2.id


def test_add_manual_requires_title(session):
    with pytest.raises(ValidationError):
        _service(session).add_manual("book", "   ")


def test_add_manual_rejects_bad_isbn(session):
    with pytest.raises(ValidationError):
        _service(session).add_manual("book", "Title", isbn="not-an-isbn")


# ── Item edit ─────────────────────────────────────────────────────────────────


def _audited_service(session):
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        source="test",
    )


def test_update_item_sets_location_and_call_number(session):
    _work, item = _audited_service(session).add_manual("book", "X")
    svc = _audited_service(session)
    updated = svc.update_item(
        item.barcode, location="Shelf A", call_number="FIC HER"
    )
    assert updated.location == "Shelf A"
    assert updated.call_number == "FIC HER"


def test_update_item_empty_string_clears_field(session):
    _, item = _audited_service(session).add_manual("book", "X", location="Shelf A")
    svc = _audited_service(session)
    updated = svc.update_item(item.barcode, location="")
    assert updated.location is None


def test_update_item_omitted_fields_unchanged(session):
    _, item = _audited_service(session).add_manual("book", "X", location="Shelf A")
    svc = _audited_service(session)
    updated = svc.update_item(item.barcode, call_number="FIC HER")
    assert updated.location == "Shelf A"
    assert updated.call_number == "FIC HER"


def test_update_item_records_audit(session):
    _, item = _audited_service(session).add_manual("book", "X")
    svc = _audited_service(session)
    svc.update_item(item.barcode, location="Shelf B", notes="dog-eared")
    entries = SqlAuditLogRepository(session).list(
        entity_type=AuditEntityType.ITEM, entity_id=item.id
    )
    update_entries = [e for e in entries if e.action == "update"]
    assert len(update_entries) == 1
    assert update_entries[0].details["changes"] == {
        "location": "Shelf B",
        "notes": "dog-eared",
    }


def test_update_item_no_changes_skips_audit(session):
    _, item = _audited_service(session).add_manual("book", "X", location="Shelf A")
    svc = _audited_service(session)
    svc.update_item(item.barcode, location="Shelf A")
    entries = SqlAuditLogRepository(session).list(
        entity_type=AuditEntityType.ITEM, entity_id=item.id
    )
    assert not any(e.action == "update" for e in entries)


def test_update_item_unknown_barcode_raises(session):
    svc = _audited_service(session)
    with pytest.raises(NotFoundError):
        svc.update_item("nonexistent", location="Shelf A")
