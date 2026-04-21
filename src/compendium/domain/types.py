from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator):
    """DateTime that always round-trips as tz-aware UTC.

    SQLite's SQLAlchemy dialect strips tzinfo on write and returns naive
    datetimes on read, even when the column is declared ``DateTime(timezone=True)``.
    This decorator normalizes both ends: aware→UTC on bind, naive→UTC on load.
    Postgres already stores ``timestamptz`` tz-aware, so this is a no-op there.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            # Treat naive inputs as already-UTC rather than guessing local TZ.
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
