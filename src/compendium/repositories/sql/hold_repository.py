from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from compendium.domain.enums import HoldStatus
from compendium.domain.models import Hold, Patron, Work

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

    # ------------------------------------------------------------------
    # Librarian list views (admin holds page, work-detail queue block)
    # ------------------------------------------------------------------

    def _active_filter(
        self,
        *,
        status: str | None,
        branch_id: int | None,
        work_id: int | None,
        patron_id: int | None,
        query: str | None,
        older_than_days: int | None,
    ):
        q = self._s.query(Hold).filter(Hold.status.not_in(_TERMINAL))
        if status:
            q = q.filter(Hold.status == status)
        if branch_id is not None:
            q = q.filter(Hold.branch_id == branch_id)
        if work_id is not None:
            q = q.filter(Hold.work_id == work_id)
        if patron_id is not None:
            q = q.filter(Hold.patron_id == patron_id)
        if older_than_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
            q = q.filter(Hold.placed_at < cutoff)
        if query:
            like = f"%{query}%"
            q = (
                q.join(Patron, Hold.patron_id == Patron.id)
                .join(Work, Hold.work_id == Work.id)
                .filter(
                    or_(
                        Patron.full_name.ilike(like),
                        Patron.library_card_number.ilike(like),
                        Work.title.ilike(like),
                    )
                )
            )
        return q

    def list_active(
        self,
        *,
        status: str | None = None,
        branch_id: int | None = None,
        work_id: int | None = None,
        patron_id: int | None = None,
        query: str | None = None,
        older_than_days: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Hold]:
        q = self._active_filter(
            status=status,
            branch_id=branch_id,
            work_id=work_id,
            patron_id=patron_id,
            query=query,
            older_than_days=older_than_days,
        ).options(
            joinedload(Hold.patron),
            joinedload(Hold.work),
            joinedload(Hold.branch),
        )
        return (
            q.order_by(Hold.placed_at.asc(), Hold.id.asc())
            .limit(limit)
            .offset(offset)
            .all()
        )

    def count_active(
        self,
        *,
        status: str | None = None,
        branch_id: int | None = None,
        work_id: int | None = None,
        patron_id: int | None = None,
        query: str | None = None,
        older_than_days: int | None = None,
    ) -> int:
        return self._active_filter(
            status=status,
            branch_id=branch_id,
            work_id=work_id,
            patron_id=patron_id,
            query=query,
            older_than_days=older_than_days,
        ).count()

    def queue_for_work(self, work_id: int) -> list[Hold]:
        """All active holds on a work, ordered by placed_at. Eager-loads
        patron for template rendering."""
        return (
            self._s.query(Hold)
            .options(joinedload(Hold.patron))
            .filter(Hold.work_id == work_id, Hold.status.not_in(_TERMINAL))
            .order_by(Hold.placed_at.asc(), Hold.id.asc())
            .all()
        )

    def queue_position(self, hold_id: int) -> int | None:
        """1-indexed position of this hold in its work's queue, or None if
        the hold is in a terminal state. AVAILABLE holds count toward the
        queue (they're ahead of all WAITING holds on the same work)."""
        hold = self.get(hold_id)
        if hold is None or hold.status in _TERMINAL:
            return None
        return (
            self._s.query(Hold)
            .filter(
                Hold.work_id == hold.work_id,
                Hold.status.not_in(_TERMINAL),
                or_(
                    Hold.placed_at < hold.placed_at,
                    and_(Hold.placed_at == hold.placed_at, Hold.id <= hold.id),
                ),
            )
            .count()
        )
