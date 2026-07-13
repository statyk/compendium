"""Aggregate availability counts for OPAC display (UX slice 7)."""
from unittest.mock import patch

import pytest

from compendium.domain.enums import ItemStatus
from compendium.domain.models import Item
from compendium.repositories.base import WorkAvailability
from compendium.repositories.sql.work_repository import SqlWorkRepository

from tests.integration.test_search import _catalog

_PAYLOAD = {
    "title": "Counts Fixture",
    "authors": [{"name": "A. Author"}],
    "publishers": [{"name": "P"}],
    "publish_date": "2000",
    "cover": {},
    "identifiers": {},
}


def _work_with_items(session, isbn: str, statuses: list[ItemStatus]):
    with patch("compendium.services.metadata.lookup_isbn", return_value=_PAYLOAD):
        _catalog(session).add_from_isbn(isbn)
    work = SqlWorkRepository(session).get_by_isbn(isbn)
    items = session.query(Item).filter_by(work_id=work.id).all()
    # add_from_isbn created one AVAILABLE item; align count and statuses.
    first = items[0]
    first.status = statuses[0].value
    for extra_status in statuses[1:]:
        session.add(
            Item(
                work_id=work.id,
                branch_id=first.branch_id,
                barcode=f"{isbn}-{extra_status.value}-{len(statuses)}",
                accession_number=f"ACC-{isbn}-{extra_status.value}",
                status=extra_status.value,
            )
        )
    session.flush()
    return work


def test_counts_mixed_statuses(session):
    work = _work_with_items(
        session,
        "9700000000012",
        [ItemStatus.AVAILABLE, ItemStatus.AVAILABLE, ItemStatus.CHECKED_OUT,
         ItemStatus.LOST, ItemStatus.WITHDRAWN],
    )
    result = SqlWorkRepository(session).availability_counts_for_works([work.id])
    assert result[work.id] == WorkAvailability(available=2, total=4, status="available")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ItemStatus.AVAILABLE, "available"),
        (ItemStatus.CHECKED_OUT, "checked_out"),
        (ItemStatus.ON_HOLD, "checked_out"),
        (ItemStatus.CLAIMS_RETURNED, "checked_out"),
        (ItemStatus.LOST, "unavailable"),
        (ItemStatus.DAMAGED, "unavailable"),
        (ItemStatus.WITHDRAWN, "unavailable"),
    ],
)
def test_status_classification_covers_every_item_status(session, status, expected):
    # Deterministic 13-digit numeric ISBN, unique per status (normalize_isbn
    # requires exactly 13 digits; status names contain letters/underscores).
    idx = list(ItemStatus).index(status)
    isbn = f"9700000{idx:06d}"
    work = _work_with_items(session, isbn, [status])
    result = SqlWorkRepository(session).availability_counts_for_works([work.id])
    av = result[work.id]
    assert av.status == expected
    assert av.total == (0 if status is ItemStatus.WITHDRAWN else 1)


def test_empty_input_and_workless_ids(session):
    repo = SqlWorkRepository(session)
    assert repo.availability_counts_for_works([]) == {}
    assert repo.availability_counts_for_works([999999]) == {}
