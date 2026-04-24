"""Cross-backend backup/restore: SQLite ↔ Postgres round-trips.

The whole point of the logical JSONL format is that the same backup file
restores on either backend. These tests stand up both sides and verify the
data arrives intact.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from alembic import command as alembic_command
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.domain.models import AppUser, Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.auth import hash_password
from compendium.services.backup import BackupService, _alembic_cfg
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService

_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}


def _seed_sample(session) -> dict:
    seed_defaults(session)
    role = SqlRoleRepository(session).get_by_name("Librarian")
    user = AppUser(
        username="admin", password_hash=hash_password("pw"), role_id=role.id
    )
    SqlUserRepository(session).add(user)
    session.flush()

    patron = Patron(library_card_number="P00042", full_name="Ada Lovelace")
    SqlPatronRepository(session).add(patron)
    session.flush()

    with patch("compendium.services.metadata.lookup_isbn", return_value=_DUNE):
        catalog = CatalogService(
            work_repo=SqlWorkRepository(session),
            item_repo=SqlItemRepository(session),
            creator_repo=SqlCreatorRepository(session),
            branch_repo=SqlBranchRepository(session),
            media_type_repo=SqlMediaTypeRepository(session),
        )
        work, item = catalog.add_from_isbn("9780441013593")

    circ = CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
    )
    loan = circ.checkout(item.barcode, patron.library_card_number)
    session.commit()
    return {
        "username": user.username,
        "card": patron.library_card_number,
        "full_name": patron.full_name,
        "work_title": work.title,
        "item_barcode": item.barcode,
        "loan_id": loan.id,
        "extra_metadata_populated": True,  # JSON column should round-trip
    }


def _init_via_alembic(url: str) -> None:
    alembic_command.upgrade(_alembic_cfg(url), "head")


def _backup_from(url: str, archive: Path) -> None:
    eng = create_engine(url)
    session = sessionmaker(bind=eng)()
    try:
        BackupService(session, Settings(database_url=url)).create(archive)
    finally:
        session.close()
        eng.dispose()


def _restore_to(url: str, archive: Path) -> None:
    eng = create_engine(url)
    session = sessionmaker(bind=eng)()
    try:
        BackupService(session, Settings(database_url=url)).restore(
            archive, include_covers=False
        )
    finally:
        session.close()
        eng.dispose()


def _verify_restored(url: str, fingerprint: dict) -> None:
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            usernames = conn.execute(
                text("SELECT username FROM app_user")
            ).scalars().all()
            assert fingerprint["username"] in usernames

            card_row = conn.execute(
                text("SELECT library_card_number, full_name FROM patron "
                     "WHERE library_card_number = :c"),
                {"c": fingerprint["card"]},
            ).first()
            assert card_row is not None
            assert card_row.full_name == fingerprint["full_name"]

            title = conn.execute(
                text("SELECT title FROM work WHERE title = :t"),
                {"t": fingerprint["work_title"]},
            ).scalar()
            assert title == fingerprint["work_title"]

            barcode = conn.execute(
                text("SELECT barcode FROM item WHERE barcode = :b"),
                {"b": fingerprint["item_barcode"]},
            ).scalar()
            assert barcode == fingerprint["item_barcode"]

            # Open loan on the restored item
            loan_count = conn.execute(
                text("SELECT COUNT(*) FROM loan WHERE returned_at IS NULL")
            ).scalar()
            assert loan_count == 1

            # JSON round-trip: Work.extra_metadata is a dict (may be empty
            # for Dune via Open Library fixture, but the column is populated
            # as a JSON document, not NULL)
            row = conn.execute(text("SELECT extra_metadata FROM work LIMIT 1")).first()
            assert row is not None
            # On Postgres: JSONB returns a dict; on SQLite: also a dict via
            # SQLAlchemy's JSON type. Just confirm it's not None.
            assert row[0] is not None
    finally:
        eng.dispose()


def _insert_pk_check(url: str) -> None:
    """After a restore, inserting a fresh row must not collide with the
    highest restored PK (tests that Postgres sequences were reset)."""
    eng = create_engine(url)
    session = sessionmaker(bind=eng)()
    try:
        new_patron = Patron(library_card_number="P99999", full_name="New User")
        session.add(new_patron)
        session.commit()
        assert new_patron.id is not None
    finally:
        session.close()
        eng.dispose()


class TestSqliteToPostgres:
    def test_roundtrip(self, tmp_path, pg_clean_url):
        src_url = f"sqlite:///{tmp_path / 'src.db'}"
        _init_via_alembic(src_url)

        src_eng = create_engine(src_url)
        src_session = sessionmaker(bind=src_eng)()
        fingerprint = _seed_sample(src_session)
        src_session.close()
        src_eng.dispose()

        archive = tmp_path / "backup.tar.gz"
        _backup_from(src_url, archive)

        _restore_to(pg_clean_url, archive)
        _verify_restored(pg_clean_url, fingerprint)
        _insert_pk_check(pg_clean_url)


class TestPostgresToSqlite:
    def test_roundtrip(self, tmp_path, pg_clean_url):
        _init_via_alembic(pg_clean_url)

        pg_eng = create_engine(pg_clean_url)
        pg_session = sessionmaker(bind=pg_eng)()
        fingerprint = _seed_sample(pg_session)
        pg_session.close()
        pg_eng.dispose()

        archive = tmp_path / "backup.tar.gz"
        _backup_from(pg_clean_url, archive)

        dst_url = f"sqlite:///{tmp_path / 'dst.db'}"
        _restore_to(dst_url, archive)
        _verify_restored(dst_url, fingerprint)
        _insert_pk_check(dst_url)
