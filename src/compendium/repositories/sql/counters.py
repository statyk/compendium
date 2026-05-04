from sqlalchemy import text
from sqlalchemy.orm import Session


class SqlCounterRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def next(self, key: str) -> int:
        """Atomically increment the counter for *key* and return the new value.

        Both Postgres and SQLite 3.35+ support UPDATE ... RETURNING.
        The INSERT ... ON CONFLICT DO NOTHING seeds the row at 0 for test DBs
        that get the schema from metadata.create_all() without running migrations.
        In production the migration pre-seeds the row so the INSERT is a no-op.
        """
        self._s.execute(
            text(
                "INSERT INTO counters (key, value) VALUES (:k, 0)"
                " ON CONFLICT (key) DO NOTHING"
            ),
            {"k": key},
        )
        result = self._s.execute(
            text("UPDATE counters SET value = value + 1 WHERE key = :k RETURNING value"),
            {"k": key},
        )
        return result.scalar_one()
