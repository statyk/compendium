from unittest.mock import patch

import pytest

from compendium.domain.errors import ExternalLookupError
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
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


@patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE)
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


@patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_add_same_isbn_twice_reuses_work(_, session):
    work1, item1 = _service(session).add_from_isbn(_ISBN)
    work2, item2 = _service(session).add_from_isbn(_ISBN)

    assert work1.id == work2.id
    assert item1.id != item2.id
    assert item2.barcode == "000002"


@patch("compendium.services.catalog.lookup_isbn", return_value={})
def test_add_from_isbn_raises_when_not_found(_, session):
    with pytest.raises(ExternalLookupError):
        _service(session).add_from_isbn(_ISBN)


@patch("compendium.services.catalog.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_add_item_to_work_adds_copy(_, session):
    work, item1 = _service(session).add_from_isbn(_ISBN)
    item2 = _service(session).add_item_to_work(work.id)

    assert item2.work_id == work.id
    assert item2.id != item1.id
