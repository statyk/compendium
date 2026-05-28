# tests/integration/test_household_permission.py
"""Integration: household.manage permission is seeded in the Librarian role."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.config.seed import seed_defaults
from compendium.domain.models import Base
from compendium.repositories.sql.role_repository import SqlRoleRepository
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
def db(engine):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    seed_defaults(s)
    s.commit()
    yield s
    s.rollback()
    s.close()


def test_librarian_has_household_manage(db):
    repo = SqlRoleRepository(db)
    librarian = repo.get_by_name("Librarian")
    assert librarian is not None
    assert "household.manage" in librarian.permissions


def test_administrator_has_household_manage(db):
    repo = SqlRoleRepository(db)
    admin = repo.get_by_name("Administrator")
    assert admin is not None
    # Administrator uses wildcard "*"
    assert "*" in admin.permissions
