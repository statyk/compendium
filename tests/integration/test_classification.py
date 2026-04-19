"""Integration tests for classification auto-population."""

from unittest.mock import patch

import pytest

from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService

_OL_DUNE_WITH_CLASSIFICATION = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
    "classifications": {
        "lc_classifications": ["PS3558.E63 D8"],
        "dewey_decimal_class": ["813.54"],
    },
}

_OL_DUNE_NO_CLASSIFICATION = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}

_ISBN = "9780441013593"


def _catalog(session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
    )


def _set_branch_scheme(session, scheme: str) -> None:
    repo = SqlBranchRepository(session)
    branch = repo.get_default()
    branch.default_classification_scheme = scheme
    repo.update(branch)
    session.flush()


def test_lcc_auto_populated_when_branch_set_to_lcc(session):
    _set_branch_scheme(session, "lcc")
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_DUNE_WITH_CLASSIFICATION):
        work, _ = _catalog(session).add_from_isbn(_ISBN)
    assert work.classification_scheme == "lcc"
    assert work.classification_code == "PS3558.E63 D8"


def test_ddc_auto_populated_when_branch_set_to_ddc(session):
    _set_branch_scheme(session, "ddc")
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_DUNE_WITH_CLASSIFICATION):
        work, _ = _catalog(session).add_from_isbn(_ISBN)
    assert work.classification_scheme == "ddc"
    assert work.classification_code == "813.54"


def test_classification_not_populated_when_scheme_is_none(session):
    _set_branch_scheme(session, "none")
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_DUNE_WITH_CLASSIFICATION):
        work, _ = _catalog(session).add_from_isbn(_ISBN)
    assert work.classification_scheme is None
    assert work.classification_code is None


def test_classification_not_populated_when_ol_missing_lcc(session):
    _set_branch_scheme(session, "lcc")
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_DUNE_NO_CLASSIFICATION):
        work, _ = _catalog(session).add_from_isbn(_ISBN)
    assert work.classification_scheme is None
    assert work.classification_code is None


def test_classification_not_populated_when_ol_missing_ddc(session):
    _set_branch_scheme(session, "ddc")
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OL_DUNE_NO_CLASSIFICATION):
        work, _ = _catalog(session).add_from_isbn(_ISBN)
    assert work.classification_scheme is None
    assert work.classification_code is None


def test_branch_scheme_update_persists(session):
    repo = SqlBranchRepository(session)
    branch = repo.get_default()
    assert branch.default_classification_scheme == "none"
    branch.default_classification_scheme = "lcc"
    repo.update(branch)
    session.flush()
    refreshed = repo.get(branch.id)
    assert refreshed.default_classification_scheme == "lcc"
