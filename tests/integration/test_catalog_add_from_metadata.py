"""CatalogService.add_from_metadata persists from a snapshot dict; fetch is separable."""
from __future__ import annotations

import pytest

from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService


def _service(session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
    )


_BOOK_META = {
    "title": "The Hobbit",
    "subtitle": None,
    "authors": ["J.R.R. Tolkien"],
    "creator_role": "author",
    "publisher": "Allen & Unwin",
    "publication_year": 1937,
    "description": None,
    "cover_image_url": None,
    "isbn": "9780261103344",
    "upc": None,
    "external_ids": {},
    "extra_metadata": {},
}


@pytest.fixture
def catalog_service_with_book_meta(session):
    return _service(session), dict(_BOOK_META)


@pytest.fixture
def catalog_service_only(session):
    return _service(session)


def test_add_from_metadata_creates_work_and_item(catalog_service_with_book_meta):
    svc, meta = catalog_service_with_book_meta
    work, item = svc.add_from_metadata(meta, media_type_code="book")
    assert work.title == meta["title"]
    assert item.work_id == work.id


def test_add_from_metadata_dedupes_existing_isbn(catalog_service_with_book_meta):
    svc, meta = catalog_service_with_book_meta
    work1, item1 = svc.add_from_metadata(meta, media_type_code="book")
    work2, item2 = svc.add_from_metadata(meta, media_type_code="book")
    assert work2.id == work1.id          # same Work
    assert item2.id != item1.id          # new copy


def test_fetch_book_metadata_returns_dict(monkeypatch, catalog_service_only):
    svc = catalog_service_only
    monkeypatch.setattr(
        "compendium.services.catalog.lookup_metadata",
        lambda *a, **k: {"title": "The Hobbit", "authors": ["Tolkien"],
                         "isbn": "9780261103344"},
    )
    # The returned dict already carries an isbn but no cover; isolate the cover
    # fallback so the unit test makes no network call.
    monkeypatch.setattr(
        "compendium.services.catalog.lookup_cover_fallbacks",
        lambda *a, **k: None,
    )
    meta = svc.fetch_book_metadata("978-0-261-10334-4")
    assert meta["title"] == "The Hobbit"
    assert meta["isbn"] == "9780261103344"
