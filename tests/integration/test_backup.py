"""End-to-end backup + restore tests.

Uses on-disk SQLite files so Alembic's ScriptDirectory can actually migrate
the target (in-memory `:memory:` DBs across connections would need
StaticPool + shared cache, which fights the way restore drops/recreates
the schema).
"""
from __future__ import annotations

import json
import tarfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from compendium.config.seed import seed_defaults
from compendium.config.settings import Settings
from compendium.domain.models import (
    AppUser,
    Base,
    Branch,
    Item,
    Loan,
    Patron,
    Role,
    Work,
)
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.auth import hash_password
from compendium.services.backup import BackupError, BackupService
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


def _make_settings(db_path: Path) -> Settings:
    return Settings(database_url=f"sqlite:///{db_path}")


def _upgraded_engine(db_path: Path):
    """Create a fresh SQLite file and run migrations to head."""
    from alembic import command as alembic_command

    from compendium.services.backup import _alembic_cfg

    url = f"sqlite:///{db_path}"
    alembic_command.upgrade(_alembic_cfg(url), "head")
    return create_engine(url)


def _seed_sample_data(session: Session) -> dict:
    """Seed defaults + a realistic slice: user, patron, work, item, loan."""
    seed_defaults(session)
    role = SqlRoleRepository(session).get_by_name("Librarian")
    user = AppUser(
        username="admin", password_hash=hash_password("pw"), role_id=role.id
    )
    SqlUserRepository(session).add(user)
    session.flush()

    patron = Patron(library_card_number="P00001", full_name="Ada")
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
        hold_repo=__import__(
            "compendium.repositories.sql.hold_repository", fromlist=["X"]
        ).SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
    )
    loan = circ.checkout(item.barcode, patron.library_card_number)
    session.commit()
    return {
        "user_username": user.username,
        "patron_card": patron.library_card_number,
        "work_title": work.title,
        "item_barcode": item.barcode,
        "loan_id": loan.id,
    }


class TestBackupCreate:
    def test_create_produces_valid_tarball(self, tmp_path):
        src_db = tmp_path / "source.db"
        engine = _upgraded_engine(src_db)
        Session_ = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        session = Session_()
        _seed_sample_data(session)
        session.close()

        session = Session_()
        svc = BackupService(session, _make_settings(src_db))
        out = tmp_path / "backup.tar.gz"
        manifest = svc.create(out)
        session.close()

        assert out.exists()
        with tarfile.open(out, "r:gz") as tar:
            names = tar.getnames()
        assert "meta.json" in names
        assert any(n.startswith("data/") and n.endswith(".jsonl") for n in names)
        assert manifest["alembic_head"]
        assert manifest["source_backend"] == "sqlite"
        assert manifest["tables"]["work"] >= 1
        assert manifest["tables"]["item"] >= 1
        assert manifest["tables"]["loan"] >= 1

    def test_create_excludes_audit_when_flagged(self, tmp_path):
        src_db = tmp_path / "source.db"
        engine = _upgraded_engine(src_db)
        Session_ = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        session = Session_()
        _seed_sample_data(session)
        session.close()

        session = Session_()
        svc = BackupService(session, _make_settings(src_db))
        out = tmp_path / "backup.tar.gz"
        manifest = svc.create(out, include_audit=False)
        session.close()

        assert manifest["include_audit"] is False
        assert manifest["tables"]["audit_log"] == 0

    def test_create_rejects_db_behind_head(self, tmp_path):
        src_db = tmp_path / "source.db"
        # Create schema but then lie about the revision
        engine = _upgraded_engine(src_db)
        from sqlalchemy import text

        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = 'cec97d4626cf'"))
        Session_ = sessionmaker(bind=engine)
        session = Session_()
        svc = BackupService(session, _make_settings(src_db))
        with pytest.raises(BackupError, match="db upgrade"):
            svc.create(tmp_path / "backup.tar.gz")
        session.close()


class TestBackupRoundtrip:
    def test_restore_into_empty_db_restores_all_data(self, tmp_path):
        src_db = tmp_path / "source.db"
        engine = _upgraded_engine(src_db)
        Session_ = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        session = Session_()
        fingerprint = _seed_sample_data(session)
        session.close()
        engine.dispose()

        # Backup
        engine = create_engine(f"sqlite:///{src_db}")
        session = sessionmaker(bind=engine)()
        svc = BackupService(session, _make_settings(src_db))
        archive = tmp_path / "backup.tar.gz"
        svc.create(archive)
        session.close()
        engine.dispose()

        # Restore into a fresh, empty SQLite DB
        dst_db = tmp_path / "target.db"
        dst_engine = create_engine(f"sqlite:///{dst_db}")
        dst_session = sessionmaker(bind=dst_engine)()
        dst_svc = BackupService(dst_session, _make_settings(dst_db))
        dst_svc.restore(archive, include_covers=False)

        # Verify data round-tripped
        with create_engine(f"sqlite:///{dst_db}").connect() as conn:
            from sqlalchemy import text

            item_rows = conn.execute(
                text("SELECT barcode FROM item")
            ).scalars().all()
            assert fingerprint["item_barcode"] in item_rows
            loan_rows = conn.execute(
                text("SELECT id, returned_at FROM loan")
            ).all()
            assert len(loan_rows) == 1
            patron_rows = conn.execute(
                text("SELECT library_card_number FROM patron")
            ).scalars().all()
            assert fingerprint["patron_card"] in patron_rows
            user_rows = conn.execute(
                text("SELECT username FROM app_user")
            ).scalars().all()
            assert fingerprint["user_username"] in user_rows
            work_rows = conn.execute(
                text("SELECT title FROM work")
            ).scalars().all()
            assert fingerprint["work_title"] in work_rows

    def test_restore_rebuilds_sqlite_fts(self, tmp_path):
        src_db = tmp_path / "source.db"
        engine = _upgraded_engine(src_db)
        Session_ = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        session = Session_()
        _seed_sample_data(session)
        session.close()
        engine.dispose()

        engine = create_engine(f"sqlite:///{src_db}")
        session = sessionmaker(bind=engine)()
        svc = BackupService(session, _make_settings(src_db))
        archive = tmp_path / "backup.tar.gz"
        svc.create(archive)
        session.close()
        engine.dispose()

        dst_db = tmp_path / "target.db"
        dst_engine = create_engine(f"sqlite:///{dst_db}")
        dst_session = sessionmaker(bind=dst_engine)()
        BackupService(dst_session, _make_settings(dst_db)).restore(
            archive, include_covers=False
        )

        # FTS should find the work
        from sqlalchemy import text
        with create_engine(f"sqlite:///{dst_db}").connect() as conn:
            hits = conn.execute(
                text("SELECT rowid FROM work_fts WHERE work_fts MATCH 'Dune'")
            ).all()
            assert len(hits) >= 1


class TestRestoreRefusals:
    def test_restore_refuses_non_empty_without_force(self, tmp_path):
        src_db = tmp_path / "source.db"
        engine = _upgraded_engine(src_db)
        Session_ = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        session = Session_()
        _seed_sample_data(session)
        session.close()
        engine.dispose()

        engine = create_engine(f"sqlite:///{src_db}")
        session = sessionmaker(bind=engine)()
        svc = BackupService(session, _make_settings(src_db))
        archive = tmp_path / "backup.tar.gz"
        svc.create(archive)
        session.close()
        engine.dispose()

        # Restore into *same* db which has real data already; should refuse
        dst_engine = create_engine(f"sqlite:///{src_db}")
        dst_session = sessionmaker(bind=dst_engine)()
        with pytest.raises(BackupError, match="--force"):
            BackupService(dst_session, _make_settings(src_db)).restore(
                archive, include_covers=False
            )

    def test_restore_with_force_overwrites(self, tmp_path):
        src_db = tmp_path / "source.db"
        engine = _upgraded_engine(src_db)
        session = sessionmaker(bind=engine)()
        _seed_sample_data(session)
        session.close()
        engine.dispose()

        engine = create_engine(f"sqlite:///{src_db}")
        session = sessionmaker(bind=engine)()
        svc = BackupService(session, _make_settings(src_db))
        archive = tmp_path / "backup.tar.gz"
        svc.create(archive)
        session.close()
        engine.dispose()

        # Set up destination DB with DIFFERENT data, then force-restore
        dst_db = tmp_path / "other.db"
        dst_engine = _upgraded_engine(dst_db)
        dst_sess = sessionmaker(bind=dst_engine)()
        seed_defaults(dst_sess)
        other = Patron(library_card_number="OTHER1", full_name="Other")
        SqlPatronRepository(dst_sess).add(other)
        dst_sess.commit()
        dst_sess.close()
        dst_engine.dispose()

        dst_engine = create_engine(f"sqlite:///{dst_db}")
        dst_session = sessionmaker(bind=dst_engine)()
        BackupService(dst_session, _make_settings(dst_db)).restore(
            archive, force=True, include_covers=False
        )

        from sqlalchemy import text
        with create_engine(f"sqlite:///{dst_db}").connect() as conn:
            cards = conn.execute(
                text("SELECT library_card_number FROM patron")
            ).scalars().all()
            assert "OTHER1" not in cards
            assert "P00001" in cards

    def test_restore_refuses_newer_head(self, tmp_path):
        src_db = tmp_path / "source.db"
        engine = _upgraded_engine(src_db)
        session = sessionmaker(bind=engine)()
        seed_defaults(session)
        session.commit()
        session.close()
        engine.dispose()

        engine = create_engine(f"sqlite:///{src_db}")
        session = sessionmaker(bind=engine)()
        svc = BackupService(session, _make_settings(src_db))
        archive = tmp_path / "backup.tar.gz"
        svc.create(archive)
        session.close()
        engine.dispose()

        # Rewrite the manifest to claim a newer revision than the code knows
        _rewrite_manifest_revision(archive, "zzzz_unknown_revision")

        dst_db = tmp_path / "target.db"
        dst_engine = create_engine(f"sqlite:///{dst_db}")
        dst_session = sessionmaker(bind=dst_engine)()
        with pytest.raises(BackupError, match="not an ancestor"):
            BackupService(dst_session, _make_settings(dst_db)).restore(
                archive, include_covers=False
            )


class TestLenientRestore:
    def test_restore_from_older_revision_upgrades_forward(self, tmp_path):
        """Backup captured at an older Alembic revision still restores
        into a fresh DB, then migrations are replayed forward."""
        src_db = tmp_path / "source.db"
        engine = _upgraded_engine(src_db)
        session = sessionmaker(bind=engine)()
        _seed_sample_data(session)
        session.close()
        engine.dispose()

        engine = create_engine(f"sqlite:///{src_db}")
        session = sessionmaker(bind=engine)()
        svc = BackupService(session, _make_settings(src_db))
        archive = tmp_path / "backup.tar.gz"
        svc.create(archive)
        session.close()
        engine.dispose()

        # Rewrite manifest to claim an older revision that IS in the chain.
        # This simulates a backup taken before a later migration shipped.
        _rewrite_manifest_revision(archive, "cec97d4626cf")  # initial schema

        dst_db = tmp_path / "target.db"
        dst_engine = create_engine(f"sqlite:///{dst_db}")
        dst_session = sessionmaker(bind=dst_engine)()
        # Insert will fail because a backup at initial_schema doesn't have
        # later-added columns — *but* the older-revision case for this test
        # is subtle: we are claiming the manifest says "older" while the
        # JSONL data is actually full-current. The restore should still
        # succeed because after alembic upgrade to source_head, the DB
        # schema matches source_head (initial_schema). The backup rows
        # have *more* columns than initial_schema has; unknown columns
        # are filtered out by the db_cols intersection.
        manifest = BackupService(dst_session, _make_settings(dst_db)).restore(
            archive, include_covers=False
        )
        assert manifest["alembic_head"] == "cec97d4626cf"

        # Target DB should now be at current head (after forward-migration)
        from compendium.services.backup import _db_revision, _current_code_head
        check_engine = create_engine(f"sqlite:///{dst_db}")
        assert _db_revision(check_engine) == _current_code_head()


class TestSafeExtract:
    """Unit-level tests for _safe_extract's traversal + symlink guards."""

    def _make_malicious_tar(self, tmp_path: Path, member_name: str, *, symlink_to: str = "") -> tarfile.TarFile:
        """Build an in-memory TarFile with a single problematic member."""
        import io
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name=member_name)
            if symlink_to:
                info.type = tarfile.SYMTYPE
                info.linkname = symlink_to
            else:
                info.size = 0
            tf.addfile(info)
        buf.seek(0)
        return tarfile.open(fileobj=buf, mode="r")

    def test_dotdot_traversal_rejected(self, tmp_path):
        from compendium.services.backup import BackupError, _safe_extract
        dest = tmp_path / "dest"
        dest.mkdir()
        with self._make_malicious_tar(tmp_path, "../evil.txt") as tar:
            with pytest.raises(BackupError, match="Unsafe path"):
                _safe_extract(tar, dest)

    def test_absolute_path_rejected(self, tmp_path):
        from compendium.services.backup import BackupError, _safe_extract
        dest = tmp_path / "dest"
        dest.mkdir()
        with self._make_malicious_tar(tmp_path, "/etc/passwd") as tar:
            with pytest.raises(BackupError, match="Unsafe path"):
                _safe_extract(tar, dest)

    def test_escaping_symlink_rejected(self, tmp_path):
        from compendium.services.backup import BackupError, _safe_extract
        dest = tmp_path / "dest"
        dest.mkdir()
        with self._make_malicious_tar(tmp_path, "link.txt", symlink_to="../../outside") as tar:
            with pytest.raises(BackupError, match="Unsafe link"):
                _safe_extract(tar, dest)

    def test_safe_member_allowed(self, tmp_path):
        from compendium.services.backup import _safe_extract
        dest = tmp_path / "dest"
        dest.mkdir()
        import io
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name="data/ok.txt")
            content = b"hello"
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r") as tar:
            _safe_extract(tar, dest)
        assert (dest / "data" / "ok.txt").read_bytes() == b"hello"


class TestBackupServiceMigrationsDirSeam:
    """Unit tests for the migrations_dir constructor argument."""

    def test_default_migrations_dir_is_inside_package(self):
        from compendium.services.backup import _MIGRATIONS_DIR
        assert _MIGRATIONS_DIR.is_dir(), f"Expected migrations dir to exist: {_MIGRATIONS_DIR}"
        assert (
            "compendium" in _MIGRATIONS_DIR.parts
        ), f"Expected migrations dir inside the compendium package: {_MIGRATIONS_DIR}"

    def test_custom_migrations_dir_is_stored(self, tmp_path):
        from unittest.mock import MagicMock
        from compendium.config.settings import Settings
        from compendium.services.backup import BackupService

        session = MagicMock()
        settings = MagicMock(spec=Settings)
        custom_dir = tmp_path / "my_migrations"

        svc = BackupService(session, settings, migrations_dir=custom_dir)
        assert svc._migrations_dir == custom_dir

    def test_none_migrations_dir_falls_back_to_default(self, tmp_path):
        from unittest.mock import MagicMock
        from compendium.config.settings import Settings
        from compendium.services.backup import BackupService, _MIGRATIONS_DIR

        session = MagicMock()
        settings = MagicMock(spec=Settings)

        svc = BackupService(session, settings, migrations_dir=None)
        assert svc._migrations_dir == _MIGRATIONS_DIR


class TestBackupServiceSettingsOptional:
    """BackupService works without a Settings object; URL is derived from the engine."""

    def test_create_and_restore_without_settings(self, tmp_path):
        src_db = tmp_path / "source.db"
        dst_db = tmp_path / "dest.db"
        archive = tmp_path / "backup.tar.gz"

        src_engine = _upgraded_engine(src_db)
        src_session = sessionmaker(bind=src_engine, autoflush=False, expire_on_commit=False)()
        _seed_sample_data(src_session)
        src_session.close()

        src_session = sessionmaker(bind=src_engine)()
        manifest = BackupService(src_session).create(archive)
        src_session.close()
        src_engine.dispose()

        assert manifest["source_backend"] == "sqlite"

        # Fresh, empty DB — restore initialises and migrates it via Alembic.
        dst_engine = create_engine(f"sqlite:///{dst_db}")
        dst_session = sessionmaker(bind=dst_engine)()
        BackupService(dst_session).restore(archive, include_covers=False)
        dst_session.close()
        dst_engine.dispose()

        restored_engine = create_engine(f"sqlite:///{dst_db}")
        with restored_engine.connect() as conn:
            from sqlalchemy import text
            count = conn.execute(text("SELECT COUNT(*) FROM work")).scalar()
        restored_engine.dispose()
        assert count >= 1

    def test_database_url_helper_prefers_settings_when_provided(self, tmp_path):
        src_db = tmp_path / "src.db"
        engine = _upgraded_engine(src_db)
        session = sessionmaker(bind=engine)()
        explicit_url = f"sqlite:///{src_db}"
        svc = BackupService(session, Settings(database_url=explicit_url))
        assert svc._database_url() == explicit_url
        session.close()
        engine.dispose()

    def test_database_url_helper_falls_back_to_engine_url(self, tmp_path):
        src_db = tmp_path / "src.db"
        engine = _upgraded_engine(src_db)
        session = sessionmaker(bind=engine)()
        svc = BackupService(session)
        url = svc._database_url()
        assert url.startswith("sqlite:///")
        assert str(src_db) in url
        session.close()
        engine.dispose()


def _rewrite_manifest_revision(archive: Path, new_revision: str) -> None:
    """Open a tar.gz backup, rewrite meta.json's alembic_head, and repack."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(td_path)
        manifest_path = td_path / "meta.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["alembic_head"] = new_revision
        manifest_path.write_text(json.dumps(manifest))
        archive.unlink()
        with tarfile.open(archive, "w:gz") as tar:
            for entry in sorted(td_path.iterdir()):
                tar.add(entry, arcname=entry.name)
