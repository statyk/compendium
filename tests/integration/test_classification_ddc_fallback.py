"""Integration tests for LoC DDC fallback in CatalogService."""

from unittest.mock import patch

from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService

_OL_NO_DDC = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {"lccn": ["65012174"]},
}

_OL_WITH_DDC = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {"lccn": ["65012174"]},
    "classifications": {"dewey_decimal_class": ["813.54"]},
}

_ISBN = "9780441013595"


def _catalog(session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
    )


def _set_ddc(session) -> None:
    repo = SqlBranchRepository(session)
    branch = repo.get_default()
    branch.default_classification_scheme = "ddc"
    repo.update(branch)
    session.flush()


def test_loc_ddc_fallback_called_when_ol_has_no_ddc(session):
    _set_ddc(session)
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_NO_DDC), \
         patch("compendium.services.catalog.lookup_ddc_from_loc", return_value="813.54") as mock_loc:
        work, _ = _catalog(session).add_from_isbn(_ISBN)
    mock_loc.assert_called_once_with(isbn=_ISBN, lccn="65012174")
    assert work.classification_scheme == "ddc"
    assert work.classification_code == "813.54"


def test_loc_ddc_fallback_not_called_when_ol_has_ddc(session):
    _set_ddc(session)
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_WITH_DDC), \
         patch("compendium.services.catalog.lookup_ddc_from_loc") as mock_loc:
        work, _ = _catalog(session).add_from_isbn(_ISBN)
    mock_loc.assert_not_called()
    assert work.classification_code == "813.54"


def test_loc_ddc_fallback_failure_leaves_classification_null(session):
    _set_ddc(session)
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_NO_DDC), \
         patch("compendium.services.catalog.lookup_ddc_from_loc", return_value=None):
        work, _ = _catalog(session).add_from_isbn(_ISBN)
    assert work.classification_scheme is None
    assert work.classification_code is None


def test_loc_ddc_fallback_not_called_for_lcc_branch(session):
    repo = SqlBranchRepository(session)
    branch = repo.get_default()
    branch.default_classification_scheme = "lcc"
    repo.update(branch)
    session.flush()
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_NO_DDC), \
         patch("compendium.services.catalog.lookup_ddc_from_loc") as mock_loc:
        _catalog(session).add_from_isbn(_ISBN)
    mock_loc.assert_not_called()
