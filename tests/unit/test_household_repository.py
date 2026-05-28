# tests/unit/test_household_repository.py
"""Unit: HouseholdRepository and PatronRepository.list_by_household."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.domain.models import Base, Household, Patron
from compendium.repositories.sql.household_repository import SqlHouseholdRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from tests.helpers import setup_sqlite_fts


@pytest.fixture(scope="module")
def engine():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(e)
    setup_sqlite_fts(e)
    return e


@pytest.fixture
def session(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.rollback()
    s.close()


class TestSqlHouseholdRepository:
    def test_add_and_get(self, session):
        repo = SqlHouseholdRepository(session)
        hh = Household(name="Adams Family")
        saved = repo.add(hh)
        assert saved.id is not None
        fetched = repo.get(saved.id)
        assert fetched.name == "Adams Family"

    def test_get_missing_returns_none(self, session):
        repo = SqlHouseholdRepository(session)
        assert repo.get(99999) is None

    def test_list_sorted_by_name(self, session):
        repo = SqlHouseholdRepository(session)
        repo.add(Household(name="Zebra House"))
        repo.add(Household(name="Apple House"))
        results = repo.list(limit=50, offset=0)
        names = [h.name for h in results]
        assert names == sorted(names)

    def test_update(self, session):
        repo = SqlHouseholdRepository(session)
        hh = repo.add(Household(name="Old Name"))
        hh.name = "New Name"
        updated = repo.update(hh)
        assert updated.name == "New Name"
        assert repo.get(hh.id).name == "New Name"

    def test_delete(self, session):
        repo = SqlHouseholdRepository(session)
        hh = repo.add(Household(name="To Delete"))
        hh_id = hh.id
        repo.delete(hh)
        assert repo.get(hh_id) is None

    def test_count(self, session):
        repo = SqlHouseholdRepository(session)
        before = repo.count()
        repo.add(Household(name="Count Test"))
        assert repo.count() == before + 1


class TestPatronListByHousehold:
    def test_returns_members(self, session):
        hh_repo = SqlHouseholdRepository(session)
        patron_repo = SqlPatronRepository(session)
        hh = hh_repo.add(Household(name="Test Household"))

        p1 = Patron(library_card_number="REPO-001", full_name="Alice", household_id=hh.id)
        p2 = Patron(library_card_number="REPO-002", full_name="Bob", household_id=hh.id)
        p3 = Patron(library_card_number="REPO-003", full_name="Carol")  # no household
        session.add_all([p1, p2, p3])
        session.flush()

        members = patron_repo.list_by_household(hh.id)
        card_nums = {m.library_card_number for m in members}
        assert "REPO-001" in card_nums
        assert "REPO-002" in card_nums
        assert "REPO-003" not in card_nums

    def test_returns_empty_for_no_members(self, session):
        hh_repo = SqlHouseholdRepository(session)
        patron_repo = SqlPatronRepository(session)
        hh = hh_repo.add(Household(name="Empty House"))
        assert patron_repo.list_by_household(hh.id) == []
