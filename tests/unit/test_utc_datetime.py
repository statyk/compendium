"""Round-trip tests for the UtcDateTime type decorator."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Column, Integer, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from compendium.domain.types import UtcDateTime


class _Base(DeclarativeBase):
    pass


class _Row(_Base):
    __tablename__ = "utc_row"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


@pytest.fixture
def sqlite_session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    _Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_aware_utc_roundtrips_aware_utc(sqlite_session):
    before = datetime(2026, 4, 20, 12, 30, 45, tzinfo=timezone.utc)
    row = _Row(ts=before)
    sqlite_session.add(row)
    sqlite_session.commit()
    sqlite_session.expire_all()

    loaded = sqlite_session.get(_Row, row.id)

    assert loaded.ts.tzinfo is not None
    assert loaded.ts.utcoffset() == timedelta(0)
    assert loaded.ts == before


def test_naive_input_treated_as_utc(sqlite_session):
    naive = datetime(2026, 4, 20, 12, 30, 45)
    row = _Row(ts=naive)
    sqlite_session.add(row)
    sqlite_session.commit()
    sqlite_session.expire_all()

    loaded = sqlite_session.get(_Row, row.id)

    assert loaded.ts == datetime(2026, 4, 20, 12, 30, 45, tzinfo=timezone.utc)


def test_non_utc_aware_input_converted_to_utc(sqlite_session):
    tz_plus_5 = timezone(timedelta(hours=5))
    local = datetime(2026, 4, 20, 17, 30, 45, tzinfo=tz_plus_5)  # = 12:30:45 UTC
    row = _Row(ts=local)
    sqlite_session.add(row)
    sqlite_session.commit()
    sqlite_session.expire_all()

    loaded = sqlite_session.get(_Row, row.id)

    assert loaded.ts == datetime(2026, 4, 20, 12, 30, 45, tzinfo=timezone.utc)


def test_none_roundtrips_none(sqlite_session):
    row = _Row(ts=None)
    sqlite_session.add(row)
    sqlite_session.commit()
    sqlite_session.expire_all()

    loaded = sqlite_session.get(_Row, row.id)

    assert loaded.ts is None


def test_compare_loaded_against_now_aware(sqlite_session):
    """The real-world motivator: comparing a DB-loaded datetime against
    ``datetime.now(timezone.utc)`` must not raise."""
    row = _Row(ts=datetime.now(timezone.utc) - timedelta(days=1))
    sqlite_session.add(row)
    sqlite_session.commit()
    sqlite_session.expire_all()

    loaded = sqlite_session.get(_Row, row.id)

    # This is what loan.py:101 does — aware < aware must work.
    assert loaded.ts < datetime.now(timezone.utc)
