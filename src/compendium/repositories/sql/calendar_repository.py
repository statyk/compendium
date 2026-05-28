from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from compendium.domain.models import ClosedDate, LibraryHours


class SqlLibraryHoursRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def list(self) -> list[LibraryHours]:
        return self._s.query(LibraryHours).order_by(LibraryHours.weekday).all()

    def get(self, weekday: int) -> LibraryHours | None:
        return self._s.get(LibraryHours, weekday)

    def update(self, hours: LibraryHours) -> LibraryHours:
        self._s.flush()
        return hours


class SqlClosedDateRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def list_in_range(self, start: date, end: date) -> list[ClosedDate]:
        """Return rows whose date range overlaps [start, end].

        Includes annually-recurring rows that have *any* year's anchor in the
        range, plus rows whose stored dates overlap the range directly.
        We fetch a broad set here; exact recurrence expansion happens in the
        service layer.
        """
        from sqlalchemy import or_

        # For annual recurrences we can't filter by exact year in SQL, so we
        # fetch all recurrence rows and let the service filter. For non-recurring
        # rows we filter to those that overlap [start, end].
        return (
            self._s.query(ClosedDate)
            .filter(
                or_(
                    ClosedDate.recurs_annually.is_(True),
                    ClosedDate.start_date <= end,
                    ClosedDate.end_date >= start,
                )
            )
            .order_by(ClosedDate.start_date)
            .all()
        )

    def get(self, closed_date_id: int) -> ClosedDate | None:
        return self._s.get(ClosedDate, closed_date_id)

    def add(self, closed_date: ClosedDate) -> ClosedDate:
        self._s.add(closed_date)
        self._s.flush()
        return closed_date

    def update(self, closed_date: ClosedDate) -> ClosedDate:
        self._s.flush()
        return closed_date

    def delete(self, closed_date: ClosedDate) -> None:
        self._s.delete(closed_date)
        self._s.flush()

    def list(self, *, limit: int = 100, offset: int = 0) -> list[ClosedDate]:
        return (
            self._s.query(ClosedDate)
            .order_by(ClosedDate.start_date)
            .limit(limit)
            .offset(offset)
            .all()
        )
