"""Shared test utilities."""
from sqlalchemy import Engine, text


def setup_sqlite_fts(engine: Engine) -> None:
    """Create FTS5 virtual table and triggers on an in-memory SQLite engine.

    Required because tests use create_all() instead of Alembic migrations.
    """
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS work_fts"
            " USING fts5(search_text, content='work', content_rowid='id')"
        ))
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS work_fts_ai AFTER INSERT ON work BEGIN"
            "  INSERT INTO work_fts(rowid, search_text)"
            "  VALUES (new.id, COALESCE(new.search_text, ''));"
            " END"
        ))
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS work_fts_ad AFTER DELETE ON work BEGIN"
            "  INSERT INTO work_fts(work_fts, rowid, search_text)"
            "  VALUES ('delete', old.id, COALESCE(old.search_text, ''));"
            " END"
        ))
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS work_fts_au AFTER UPDATE OF search_text ON work BEGIN"
            "  INSERT INTO work_fts(work_fts, rowid, search_text)"
            "  VALUES ('delete', old.id, COALESCE(old.search_text, ''));"
            "  INSERT INTO work_fts(rowid, search_text)"
            "  VALUES (new.id, COALESCE(new.search_text, ''));"
            " END"
        ))
        conn.commit()
