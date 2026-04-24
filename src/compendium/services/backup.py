"""Backup and restore via portable JSONL dumps.

Format is backend-agnostic so a backup taken on SQLite restores to Postgres
(and vice versa). A `meta.json` manifest records the Alembic revision that
produced the backup; on restore we migrate the target DB to that revision,
insert the rows, then replay migrations forward to current head.
"""
from __future__ import annotations

import importlib.metadata
import json
import shutil
import tarfile
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import sqlalchemy as sa
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.types import TypeDecorator

from compendium.config.settings import Settings
from compendium.domain.models import Base
from compendium.services.covers import cache_dir as cover_cache_dir

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS_DIR = _PROJECT_ROOT / "migrations"

_MANIFEST_NAME = "meta.json"
_DATA_DIR = "data"
_COVERS_DIR = "covers"

_AUDIT_TABLE = "audit_log"
_SEED_TABLES = frozenset({"media_type", "branch", "patron_category", "role", "loan_policy"})

_BATCH_SIZE = 500


class BackupError(Exception):
    """Backup or restore cannot proceed."""


def _alembic_cfg(db_url: str) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _current_code_head() -> str:
    script = ScriptDirectory.from_config(_alembic_cfg("sqlite://"))
    heads = script.get_heads()
    if len(heads) != 1:
        raise BackupError(f"Expected one Alembic head, got {heads}")
    return heads[0]


def _db_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _detect_backend(url: str) -> str:
    if url.startswith("sqlite"):
        return "sqlite"
    if url.startswith("postgres"):
        return "postgres"
    return "unknown"


def _compendium_version() -> str:
    try:
        return importlib.metadata.version("compendium")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _unwrap_type(col_type: Any) -> Any:
    while isinstance(col_type, TypeDecorator):
        col_type = col_type.impl_instance
    return col_type


def _encode_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _decode_value(col: sa.Column, value: Any) -> Any:
    if value is None:
        return None
    base = _unwrap_type(col.type)
    if isinstance(base, sa.DateTime) and isinstance(value, str):
        return datetime.fromisoformat(value)
    if isinstance(base, sa.Date) and isinstance(value, str):
        return date.fromisoformat(value)
    # JSON columns: raw SQL binds don't trigger the dialect's JSON serializer,
    # so hand SQLite/Postgres a JSON string (Postgres JSONB parses it).
    if isinstance(base, sa.JSON) and isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _is_ancestor(revision: str, descendant: str, cfg: AlembicConfig) -> bool:
    """Return True if `revision` appears in the chain reaching `descendant`."""
    if revision == descendant:
        return True
    script = ScriptDirectory.from_config(cfg)
    try:
        for rev in script.walk_revisions("base", descendant):
            if rev.revision == revision:
                return True
    except Exception:
        return False
    return False


def _iter_user_tables_sqlite(conn: sa.Connection) -> Iterator[str]:
    rows = conn.execute(
        text(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ).all()
    for (name,) in rows:
        yield name


def _clear_target(engine: Engine) -> None:
    dialect = engine.dialect.name
    if dialect == "sqlite":
        with engine.begin() as conn:
            for name in list(_iter_user_tables_sqlite(conn)):
                conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
    elif dialect == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    else:
        raise BackupError(f"Unsupported database dialect: {dialect}")


def _has_real_data(engine: Engine) -> bool:
    """Return True if any non-seed table has at least one row."""
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if table.name in _SEED_TABLES:
            continue
        if not insp.has_table(table.name):
            continue
        with engine.connect() as conn:
            row = conn.execute(select(table).limit(1)).first()
            if row is not None:
                return True
    return False


def _delete_all_rows(engine: Engine) -> None:
    """Delete every row from every user table (reverse FK order), preserving schema."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            if insp.has_table(table.name):
                conn.execute(text(f'DELETE FROM "{table.name}"'))


def _rebuild_sqlite_fts(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_fts'")
        ).first()
        if exists:
            conn.execute(text("INSERT INTO work_fts(work_fts) VALUES('rebuild')"))


class BackupService:
    def __init__(self, session: Session, settings: Settings):
        self._session = session
        self._settings = settings

    # ----- create -------------------------------------------------------------

    def create(
        self,
        output_path: Path,
        *,
        include_covers: bool = True,
        include_audit: bool = True,
    ) -> dict[str, Any]:
        engine = self._session.get_bind()
        revision = _db_revision(engine)
        head = _current_code_head()
        if revision is None:
            raise BackupError(
                "Target database has no alembic_version row. "
                "Run 'compendium db init' first."
            )
        if revision != head:
            raise BackupError(
                f"Database is at revision {revision}, but the code's head is {head}. "
                "Run 'compendium db upgrade' before taking a backup."
            )

        counts: dict[str, int] = {}
        output_path = Path(output_path).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as td:
            staging = Path(td)
            data_dir = staging / _DATA_DIR
            data_dir.mkdir()

            for idx, table in enumerate(Base.metadata.sorted_tables, start=1):
                if table.name == _AUDIT_TABLE and not include_audit:
                    counts[table.name] = 0
                    continue
                counts[table.name] = self._dump_table(table, data_dir, idx)

            if include_covers:
                src = cover_cache_dir()
                if src.exists() and any(src.iterdir()):
                    shutil.copytree(src, staging / _COVERS_DIR)

            manifest = {
                "compendium_version": _compendium_version(),
                "alembic_head": revision,
                "source_backend": _detect_backend(self._settings.database_url),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "tables": counts,
                "include_audit": include_audit,
                "include_covers": include_covers,
            }
            (staging / _MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))

            with tarfile.open(output_path, "w:gz") as tar:
                for entry in sorted(staging.iterdir()):
                    tar.add(entry, arcname=entry.name)

        return manifest

    def _dump_table(self, table: sa.Table, data_dir: Path, idx: int) -> int:
        path = data_dir / f"{idx:03d}_{table.name}.jsonl"
        n = 0
        with path.open("w", encoding="utf-8") as f:
            for row in self._session.execute(select(table)):
                record = {
                    col.name: _encode_value(row._mapping[col.name])
                    for col in table.columns
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                n += 1
        return n

    # ----- restore ------------------------------------------------------------

    def restore(
        self,
        input_path: Path,
        *,
        force: bool = False,
        include_covers: bool = True,
    ) -> dict[str, Any]:
        input_path = Path(input_path).expanduser().resolve()
        if not input_path.exists():
            raise BackupError(f"Backup file not found: {input_path}")

        engine = self._session.get_bind()
        db_url = self._settings.database_url

        with tempfile.TemporaryDirectory() as td:
            staging = Path(td)
            with tarfile.open(input_path, "r:gz") as tar:
                _safe_extract(tar, staging)

            manifest_path = staging / _MANIFEST_NAME
            if not manifest_path.exists():
                raise BackupError(f"Not a Compendium backup: missing {_MANIFEST_NAME}")
            manifest = json.loads(manifest_path.read_text())
            source_head = manifest.get("alembic_head")
            if not source_head:
                raise BackupError("Backup manifest has no alembic_head.")

            current_head = _current_code_head()
            if source_head != current_head:
                if not _is_ancestor(source_head, current_head, _alembic_cfg(db_url)):
                    raise BackupError(
                        f"Backup was taken at revision {source_head}, which is not "
                        f"an ancestor of this code's head ({current_head}). Upgrade "
                        "Compendium to a version that includes that revision before "
                        "restoring."
                    )

            if not force and _has_real_data(engine):
                raise BackupError(
                    "Target database has existing data. Pass --force to overwrite."
                )

            # Drop existing session state so we can safely wipe + recreate schema.
            self._session.rollback()
            self._session.close()

            _clear_target(engine)
            alembic_command.upgrade(_alembic_cfg(db_url), source_head)
            # Migrations sometimes seed reference data (e.g. patron_category
            # rows added by b8c9d0e1f2a3). The backup already contains those
            # rows with their original PKs; wipe anything the migrations
            # inserted so the restore can re-create them verbatim.
            _delete_all_rows(engine)

            self._insert_all(staging / _DATA_DIR, engine)

            if engine.dialect.name == "postgresql":
                with engine.begin() as conn:
                    _reset_postgres_sequences_conn(conn)

            if source_head != current_head:
                alembic_command.upgrade(_alembic_cfg(db_url), "head")

            _rebuild_sqlite_fts(engine)

            if include_covers:
                src = staging / _COVERS_DIR
                if src.exists():
                    dst = cover_cache_dir()
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)

        return manifest

    def _insert_all(self, data_dir: Path, engine: Engine) -> None:
        if not data_dir.exists():
            raise BackupError(f"Backup is missing {_DATA_DIR}/ directory.")
        insp = inspect(engine)
        tables_by_name = {t.name: t for t in Base.metadata.sorted_tables}
        files = sorted(data_dir.glob("*.jsonl"))
        with engine.begin() as conn:
            for path in files:
                _, _, table_name = path.stem.partition("_")
                table = tables_by_name.get(table_name)
                if table is None or not insp.has_table(table_name):
                    continue
                db_cols = {c["name"] for c in insp.get_columns(table_name)}
                self._insert_file(conn, table, path, db_cols)

    def _insert_file(
        self,
        conn: sa.Connection,
        table: sa.Table,
        path: Path,
        db_cols: set[str],
    ) -> None:
        col_by_name = {c.name: c for c in table.columns}
        batch: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                row: dict[str, Any] = {}
                for key, raw in record.items():
                    if key not in db_cols:
                        continue
                    col = col_by_name.get(key)
                    row[key] = _decode_value(col, raw) if col is not None else raw
                batch.append(row)
                if len(batch) >= _BATCH_SIZE:
                    self._flush(conn, table, batch, db_cols)
                    batch = []
            if batch:
                self._flush(conn, table, batch, db_cols)

    @staticmethod
    def _flush(
        conn: sa.Connection,
        table: sa.Table,
        batch: list[dict[str, Any]],
        db_cols: set[str],
    ) -> None:
        if not batch:
            return
        # Union the keys actually present in the batch, intersected with the
        # target DB's columns — guards against older schemas that don't have
        # columns added in later migrations.
        keys = sorted({k for row in batch for k in row.keys()} & db_cols)
        for row in batch:
            for k in keys:
                row.setdefault(k, None)
        col_list = ", ".join(f'"{k}"' for k in keys)
        placeholders = ", ".join(f":{k}" for k in keys)
        sql = text(f'INSERT INTO "{table.name}" ({col_list}) VALUES ({placeholders})')
        conn.execute(sql, batch)


def _reset_postgres_sequences_conn(conn: sa.Connection) -> None:
    for table in Base.metadata.sorted_tables:
        pk_cols = [
            c for c in table.primary_key.columns
            if isinstance(_unwrap_type(c.type), sa.Integer)
        ]
        if len(pk_cols) != 1:
            continue
        col = pk_cols[0]
        conn.execute(
            text(
                "SELECT CASE WHEN pg_get_serial_sequence(:t, :c) IS NOT NULL "
                f"THEN setval(pg_get_serial_sequence(:t, :c), "
                f"COALESCE((SELECT MAX(\"{col.name}\") FROM \"{table.name}\"), 0) + 1, false) END"
            ),
            {"t": table.name, "c": col.name},
        )


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Reject absolute paths and traversal attempts before extraction."""
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest)):
            raise BackupError(f"Unsafe path in archive: {member.name}")
    tar.extractall(dest)
