"""Smoke tests verifying core paths work against a real Postgres backend.

These tests mirror the integration tests in tests/integration/ but run against
the pg_session fixture (testcontainers Postgres). The `session` fixture is
overridden in tests/postgres/conftest.py to use pg_session.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from compendium.domain.models import Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
from compendium.services.holds import HoldService

_OPEN_LIB_DUNE = {
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


def _circulation(session) -> CirculationService:
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
    )


def _holds(session) -> HoldService:
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
    )


# ── catalog ──────────────────────────────────────────────────────────────────

@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_pg_add_from_isbn_creates_work_and_item(_, session):
    work, item = _catalog(session).add_from_isbn(_ISBN)

    assert work.title == "Dune"
    assert work.isbn == _ISBN
    assert item.status == "available"
    assert item.work_id == work.id


@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_pg_add_same_isbn_twice_reuses_work(_, session):
    work1, item1 = _catalog(session).add_from_isbn(_ISBN)
    work2, item2 = _catalog(session).add_from_isbn(_ISBN)

    assert work1.id == work2.id
    assert item1.id != item2.id


# ── datetime timezone awareness ───────────────────────────────────────────────

@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_pg_timestamps_are_timezone_aware(_, session):
    work, item = _catalog(session).add_from_isbn(_ISBN)
    session.refresh(work)
    assert work.created_at.tzinfo is not None


# ── circulation ───────────────────────────────────────────────────────────────

@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_pg_checkout_and_checkin(_, session):
    _, item = _catalog(session).add_from_isbn(_ISBN)
    patron = Patron(library_card_number="PG0001", full_name="PG Patron")
    SqlPatronRepository(session).add(patron)
    session.flush()

    circ = _circulation(session)
    loan = circ.checkout(item.barcode, patron.library_card_number)
    assert loan.returned_at is None
    assert loan.checked_out_at.tzinfo is not None
    assert loan.due_at.tzinfo is not None

    returned = circ.checkin(item.barcode)
    assert returned.returned_at is not None
    assert returned.returned_at.tzinfo is not None


# ── holds ─────────────────────────────────────────────────────────────────────

@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_pg_place_and_cancel_hold(_, session):
    work, _ = _catalog(session).add_from_isbn(_ISBN)
    patron = Patron(library_card_number="PG0002", full_name="PG Patron 2")
    SqlPatronRepository(session).add(patron)
    session.flush()

    svc = _holds(session)
    hold = svc.place(work.id, patron.library_card_number)
    assert hold.status == "waiting"
    assert hold.placed_at.tzinfo is not None

    cancelled = svc.cancel(hold.id, patron.id)
    assert cancelled.status == "cancelled"


# ── JSON columns ──────────────────────────────────────────────────────────────

@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_pg_json_columns_round_trip(_, session):
    work, _ = _catalog(session).add_from_isbn(_ISBN)
    session.refresh(work)
    assert isinstance(work.external_ids, dict)
    assert isinstance(work.extra_metadata, dict)


# ── full-text search (Postgres tsvector/GIN) ──────────────────────────────────

_OPEN_LIB_FOUNDATION = {
    "title": "Foundation",
    "authors": [{"name": "Isaac Asimov"}],
    "publishers": [{"name": "Gnome Press"}],
    "publish_date": "1951",
    "cover": {},
    "identifiers": {},
}
_ISBN_FOUNDATION = "9780553293357"


@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_pg_fts_finds_by_title(_, session):
    _catalog(session).add_from_isbn(_ISBN)
    session.flush()
    results = SqlWorkRepository(session).search("Dune")
    assert any(w.title == "Dune" for w in results)


@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_FOUNDATION)
def test_pg_fts_finds_by_author(_, session):
    _catalog(session).add_from_isbn(_ISBN_FOUNDATION)
    session.flush()
    results = SqlWorkRepository(session).search("Asimov")
    assert any(w.title == "Foundation" for w in results)


@patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE)
def test_pg_search_text_populated(_, session):
    work, _ = _catalog(session).add_from_isbn("9780441013595")
    session.refresh(work)
    assert work.search_text is not None
    assert "Dune" in work.search_text
    assert "Frank Herbert" in work.search_text
