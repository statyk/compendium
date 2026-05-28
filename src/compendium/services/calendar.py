"""CalendarService — library hours, closed dates, and due-date rolling.

All public methods that accept or return datetimes work in UTC.
Local-date arithmetic is done internally via the configured IANA timezone.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import ClosedDate, LibraryHours
from compendium.repositories.base import ClosedDateRepository, LibraryHoursRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService


class NoOpenDayError(BusinessRuleError):
    """Raised when next_open_date cannot find an open day within 366 days."""


_MAX_ROLL_DAYS = 366  # safety ceiling for the roll-forward loop
_DEFAULT_CLOSE_TIME = time(23, 59)


class CalendarService:
    def __init__(
        self,
        hours_repo: LibraryHoursRepository,
        closed_date_repo: ClosedDateRepository,
        timezone: str = "UTC",
        audit_svc: AuditService | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._hours = hours_repo
        self._closed = closed_date_repo
        self._tz_name = timezone
        self._audit = audit_svc
        self._actor_label = actor_label
        self._source = source

    # ------------------------------------------------------------------
    # Timezone helper
    # ------------------------------------------------------------------

    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self._tz_name)

    # ------------------------------------------------------------------
    # Closed-date expansion helper
    # ------------------------------------------------------------------

    def _is_closed_date(self, d: date, closed_dates: list[ClosedDate]) -> bool:
        """Return True if *d* falls inside any entry in *closed_dates*."""
        for cd in closed_dates:
            if cd.recurs_annually:
                # Match on month/day in the queried year
                start_this = cd.start_date.replace(year=d.year)
                delta_days = (cd.end_date - cd.start_date).days
                end_this = start_this + timedelta(days=delta_days)
                if start_this <= d <= end_this:
                    return True
            else:
                if cd.start_date <= d <= cd.end_date:
                    return True
        return False

    # ------------------------------------------------------------------
    # Hours cache (avoid repeated DB round-trips within one request)
    # ------------------------------------------------------------------

    def _weekday_map(self) -> dict[int, LibraryHours]:
        return {h.weekday: h for h in self._hours.list()}

    # ------------------------------------------------------------------
    # Public query methods
    # ------------------------------------------------------------------

    def is_open_on(self, d: date) -> bool:
        """True if the library is open on local date *d*."""
        wmap = self._weekday_map()
        # weekday(): Mon=0 … Sun=6  (ISO Python convention matches our schema)
        row = wmap.get(d.weekday())
        if row is None or not row.is_open:
            return False
        # Fetch closed-date overrides for this single day
        closed = self._closed.list_in_range(d, d)
        return not self._is_closed_date(d, closed)

    def next_open_date(self, d: date) -> date:
        """Return *d* if open, otherwise roll forward to the next open day.

        Raises NoOpenDayError if no open day is found within 366 days.
        """
        wmap = self._weekday_map()
        # Fetch a broad range of closed dates to avoid repeated DB calls
        end = d + timedelta(days=_MAX_ROLL_DAYS)
        closed = self._closed.list_in_range(d, end)

        current = d
        for _ in range(_MAX_ROLL_DAYS + 1):
            row = wmap.get(current.weekday())
            if row is not None and row.is_open and not self._is_closed_date(current, closed):
                return current
            current += timedelta(days=1)

        raise NoOpenDayError(
            "No open day found within 366 days — please check library hours configuration."
        )

    def compute_due_at(self, checkout_utc: datetime, period_days: int) -> datetime:
        """Compute a due-date UTC instant.

        Adds *period_days* as calendar days from the checkout moment (in
        local time), then rolls forward past any closed days, and returns
        the UTC equivalent of that open day's close_time.
        """
        tz = self._tz()
        local_checkout = checkout_utc.astimezone(tz)
        naive_due_local = local_checkout.date() + timedelta(days=period_days)
        open_day = self.next_open_date(naive_due_local)

        wmap = self._weekday_map()
        row = wmap.get(open_day.weekday())
        close = row.close_time if (row and row.close_time) else _DEFAULT_CLOSE_TIME

        # Build aware local datetime, then convert to UTC
        local_due = datetime(
            open_day.year, open_day.month, open_day.day,
            close.hour, close.minute,
            tzinfo=tz,
        )
        return local_due.astimezone(timezone.utc)

    def closed_days_between(self, start_utc: datetime, end_utc: datetime) -> int:
        """Count closed local-dates in the half-open interval [start_utc, end_utc).

        Used by FineService to subtract non-chargeable days from days_over.
        """
        if end_utc <= start_utc:
            return 0
        tz = self._tz()
        start_local = start_utc.astimezone(tz).date()
        end_local = end_utc.astimezone(tz).date()

        # Inclusive range of local dates to check
        if end_local <= start_local:
            return 0

        closed_rows = self._closed.list_in_range(start_local, end_local)
        wmap = self._weekday_map()

        count = 0
        current = start_local
        while current < end_local:
            row = wmap.get(current.weekday())
            if row is None or not row.is_open or self._is_closed_date(current, closed_rows):
                count += 1
            current += timedelta(days=1)
        return count

    # ------------------------------------------------------------------
    # CRUD — Library Hours
    # ------------------------------------------------------------------

    def get_hours(self) -> list[LibraryHours]:
        return self._hours.list()

    def update_weekday(
        self,
        weekday: int,
        *,
        is_open: bool | None = None,
        open_time: time | None | type[...] = ...,
        close_time: time | None | type[...] = ...,
    ) -> LibraryHours:
        if weekday < 0 or weekday > 6:
            raise ValidationError("weekday must be 0–6 (Mon–Sun)")
        row = self._hours.get(weekday)
        if row is None:
            raise NotFoundError(f"No library_hours row for weekday={weekday}")
        before = {"is_open": row.is_open, "open_time": _t(row.open_time), "close_time": _t(row.close_time)}
        if is_open is not None:
            row.is_open = is_open
        if open_time is not ...:
            row.open_time = open_time  # type: ignore[assignment]
        if close_time is not ...:
            row.close_time = close_time  # type: ignore[assignment]
        self._hours.update(row)
        if self._audit:
            self._audit.record(
                actor=None,
                actor_label=self._actor_label,
                source=self._source,
                entity_type=AuditEntityType.LIBRARY_HOURS,
                entity_id=weekday,
                action=AuditAction.UPDATE,
                details={"before": before, "after": {"is_open": row.is_open, "open_time": _t(row.open_time), "close_time": _t(row.close_time)}},
            )
        return row

    # ------------------------------------------------------------------
    # CRUD — Closed Dates
    # ------------------------------------------------------------------

    def list_closed_dates(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[ClosedDate]:
        return self._closed.list(limit=limit, offset=offset)

    def add_closed_date(
        self,
        start_date: date,
        end_date: date | None = None,
        label: str | None = None,
        *,
        recurs_annually: bool = False,
    ) -> ClosedDate:
        if end_date is None:
            end_date = start_date
        if end_date < start_date:
            raise ValidationError("end_date must not be before start_date")
        cd = ClosedDate(
            start_date=start_date,
            end_date=end_date,
            label=label,
            recurs_annually=recurs_annually,
        )
        self._closed.add(cd)
        if self._audit:
            self._audit.record(
                actor=None,
                actor_label=self._actor_label,
                source=self._source,
                entity_type=AuditEntityType.CLOSED_DATE,
                entity_id=cd.id,
                action=AuditAction.CREATE,
                details={"start": str(start_date), "end": str(end_date), "label": label, "recurs": recurs_annually},
            )
        return cd

    def update_closed_date(
        self,
        closed_date_id: int,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        label: str | None | type[...] = ...,
        recurs_annually: bool | None = None,
    ) -> ClosedDate:
        cd = self._closed.get(closed_date_id)
        if cd is None:
            raise NotFoundError(f"No closed_date with id={closed_date_id}")
        if start_date is not None:
            cd.start_date = start_date
        if end_date is not None:
            cd.end_date = end_date
        if cd.end_date < cd.start_date:
            raise ValidationError("end_date must not be before start_date")
        if label is not ...:
            cd.label = label  # type: ignore[assignment]
        if recurs_annually is not None:
            cd.recurs_annually = recurs_annually
        self._closed.update(cd)
        return cd

    def delete_closed_date(self, closed_date_id: int) -> None:
        cd = self._closed.get(closed_date_id)
        if cd is None:
            raise NotFoundError(f"No closed_date with id={closed_date_id}")
        self._closed.delete(cd)


def _t(t: time | None) -> str | None:
    return t.strftime("%H:%M") if t else None
