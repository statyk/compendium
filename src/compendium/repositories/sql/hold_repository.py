from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from compendium.domain.enums import HoldStatus
from compendium.domain.models import Hold

_TERMINAL = [HoldStatus.FULFILLED.value, HoldStatus.CANCELLED.value, HoldStatus.EXPIRED.value]


def _not_suspended_today():
    """Filter expression for 'hold is not currently suspended'. A hold is
    considered active in the queue when suspended_until is NULL (never
    suspended) or past today (expired suspension). Callers should additionally
    filter on status=WAITING; this just handles the suspension overlay."""
    today = date.today()
    return or_(Hold.suspended_until.is_(None), Hold.suspended_until <= today)


class SqlHoldRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, hold: Hold) -> Hold:
        self._s.add(hold)
        self._s.flush()
        return hold

    def get(self, hold_id: int) -> Hold | None:
        return self._s.get(Hold, hold_id)

    def get_active_for_patron(self, patron_id: int) -> list[Hold]:
        return (
            self._s.query(Hold)
            .filter(Hold.patron_id == patron_id, Hold.status.not_in(_TERMINAL))
            .all()
        )

    def get_active_for_work(self, work_id: int) -> list[Hold]:
        return (
            self._s.query(Hold)
            .filter(Hold.work_id == work_id, Hold.status.not_in(_TERMINAL))
            .order_by(Hold.placed_at.asc())
            .all()
        )

    def get_oldest_waiting_for_work(self, work_id: int) -> Hold | None:
        # Suspended holds are skipped — the queue promotes the next
        # non-suspended hold when a copy becomes available.
        return (
            self._s.query(Hold)
            .filter(
                Hold.work_id == work_id,
                Hold.status == HoldStatus.WAITING.value,
                _not_suspended_today(),
            )
            .order_by(Hold.placed_at.asc())
            .first()
        )

    def list_suspended_expiring_on_or_before(self, cutoff: date) -> list[Hold]:
        """Active suspended holds whose suspended_until has passed. Used by
        the maintenance resumer to auto-resume on or after the end date."""
        return (
            self._s.query(Hold)
            .filter(
                Hold.status == HoldStatus.WAITING.value,
                Hold.suspended_until.is_not(None),
                Hold.suspended_until <= cutoff,
            )
            .order_by(Hold.suspended_until.asc(), Hold.id.asc())
            .all()
        )

    def get_available_for_patron_work(self, patron_id: int, work_id: int) -> Hold | None:
        return (
            self._s.query(Hold)
            .filter(
                Hold.patron_id == patron_id,
                Hold.work_id == work_id,
                Hold.status == HoldStatus.AVAILABLE.value,
            )
            .first()
        )

    def get_active_for_patron_work(self, patron_id: int, work_id: int) -> Hold | None:
        return (
            self._s.query(Hold)
            .filter(
                Hold.patron_id == patron_id,
                Hold.work_id == work_id,
                Hold.status.not_in(_TERMINAL),
            )
            .first()
        )

    def get_expired_waiting(self, before: datetime) -> list[Hold]:
        """Return holds past their expires_at — covers both WAITING queue
        expiry and AVAILABLE pickup-window expiry."""
        return (
            self._s.query(Hold)
            .filter(
                Hold.status.in_([HoldStatus.WAITING.value, HoldStatus.AVAILABLE.value]),
                Hold.expires_at.is_not(None),
                Hold.expires_at < before,
            )
            .all()
        )

    def update(self, hold: Hold) -> Hold:
        self._s.flush()
        return hold
