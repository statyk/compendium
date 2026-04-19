"""Integration tests for LoC LCC fallback in CatalogService."""

from unittest.mock import patch

from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService

_OL_NO_CLASSIFICATION = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {"lccn": ["65012174"]},
}

_OL_NO_CLASSIFICATION_NO_LCCN = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}

_OL_WITH_LCC = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {"lccn": ["65012174"]},
    "classifications": {"lc_classifications": ["PS3558.E63 D8"]},
}

_ISBN = "9780441013594"


def _catalog(session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
    )


def _set_lcc(session) -> None:
    repo = SqlBranchRepository(session)
    branch = repo.get_default()
    branch.default_classification_scheme = "lcc"
    repo.update(branch)
    session.flush()


def test_loc_fallback_called_when_ol_has_no_lcc(session):
    _set_lcc(session)
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_NO_CLASSIFICATION), \
         patch("compendium.services.metadata.lookup_lcc_from_loc", return_value="PS3558.E63 D8") as mock_loc:
        work, _ = _catalog(session).add_from_isbn(_ISBN)
    mock_loc.assert_called_once_with(isbn=_ISBN, lccn="65012174")
    assert work.classification_scheme == "lcc"
    assert work.classification_code == "PS3558.E63 D8"


def test_loc_fallback_not_called_when_ol_has_lcc(session):
    _set_lcc(session)
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_WITH_LCC), \
         patch("compendium.services.metadata.lookup_lcc_from_loc") as mock_loc:
        work, _ = _catalog(session).add_from_isbn(_ISBN)
    mock_loc.assert_not_called()
    assert work.classification_code == "PS3558.E63 D8"


def test_loc_fallback_with_isbn_when_no_lccn(session):
    _set_lcc(session)
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_NO_CLASSIFICATION_NO_LCCN), \
         patch("compendium.services.metadata.lookup_lcc_from_loc", return_value="PS3558.E63") as mock_loc:
        work, _ = _catalog(session).add_from_isbn(_ISBN)
    mock_loc.assert_called_once_with(isbn=_ISBN, lccn=None)
    assert work.classification_code == "PS3558.E63"


def test_loc_fallback_failure_leaves_classification_null(session):
    _set_lcc(session)
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_NO_CLASSIFICATION), \
         patch("compendium.services.metadata.lookup_lcc_from_loc", return_value=None):
        work, _ = _catalog(session).add_from_isbn(_ISBN)
    assert work.classification_scheme is None
    assert work.classification_code is None


def test_loc_fallback_not_called_for_ddc_branch(session):
    repo = SqlBranchRepository(session)
    branch = repo.get_default()
    branch.default_classification_scheme = "ddc"
    repo.update(branch)
    session.flush()

    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_NO_CLASSIFICATION), \
         patch("compendium.services.metadata.lookup_lcc_from_loc") as mock_loc:
        _catalog(session).add_from_isbn(_ISBN)
    mock_loc.assert_not_called()
