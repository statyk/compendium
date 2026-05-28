"""Integration: Household model persistence and patron linkage."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.config.seed import seed_defaults
from compendium.domain.models import Base, Household, Patron
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from tests.helpers import setup_sqlite_fts


@pytest.fixture(scope="module")
def hh_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    setup_sqlite_fts(engine)
    return engine


@pytest.fixture
def db(hh_engine):
    factory = sessionmaker(bind=hh_engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


def test_create_household(db):
    hh = Household(name="Smith Family")
    db.add(hh)
    db.flush()
    assert hh.id is not None
    assert hh.name == "Smith Family"
    assert hh.notes is None


def test_household_nullable_name(db):
    hh = Household(name="Unnamed")
    db.add(hh)
    db.flush()
    assert hh.name == "Unnamed"


def test_patron_household_link(db):
    hh = Household(name="Jones Family")
    db.add(hh)
    db.flush()

    patron = Patron(
        library_card_number="HH-TEST-001",
        full_name="Alice Jones",
        household_id=hh.id,
    )
    db.add(patron)
    db.flush()
    db.refresh(patron)

    assert patron.household_id == hh.id
    assert patron.household.name == "Jones Family"
    assert patron in hh.members


def test_patron_household_nullable(db):
    patron = Patron(library_card_number="HH-TEST-002", full_name="No Family")
    db.add(patron)
    db.flush()
    assert patron.household_id is None
    assert patron.household is None
