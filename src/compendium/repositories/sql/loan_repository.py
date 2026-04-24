from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from compendium.domain.models import Item, Loan, Patron, Work


class SqlLoanRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, loan: Loan) -> Loan:
        self._s.add(loan)
        self._s.flush()
        return loan

    def get(self, loan_id: int) -> Loan | None:
        return self._s.get(Loan, loan_id)

    def get_active_for_item(self, item_id: int) -> Loan | None:
        return (
            self._s.query(Loan).filter(Loan.item_id == item_id, Loan.returned_at.is_(None)).first()
        )

    def get_active_for_patron(self, patron_id: int) -> list[Loan]:
        return (
            self._s.query(Loan)
            .filter(Loan.patron_id == patron_id, Loan.returned_at.is_(None))
            .all()
        )

    def get_most_recent_for_item(self, item_id: int) -> Loan | None:
        """Return the most recently-started loan for this item, active or returned."""
        return (
            self._s.query(Loan)
            .filter(Loan.item_id == item_id)
            .order_by(Loan.checked_out_at.desc())
            .first()
        )

    def list_active_overdue(self, *, patron_id: int | None = None) -> list[Loan]:
        """Active loans (returned_at IS NULL) whose due_at has passed."""
        now = datetime.now(tz=timezone.utc)
        q = self._s.query(Loan).filter(
            Loan.returned_at.is_(None),
            Loan.due_at < now,
        )
        if patron_id is not None:
            q = q.filter(Loan.patron_id == patron_id)
        return q.order_by(Loan.due_at).all()

    def list_due_within(self, *, days: int) -> list[Loan]:
        """Active loans whose due_at is in the future but within `days` days."""
        now = datetime.now(tz=timezone.utc)
        return (
            self._s.query(Loan)
            .filter(
                Loan.returned_at.is_(None),
                Loan.due_at > now,
                Loan.due_at <= now + timedelta(days=days),
            )
            .order_by(Loan.due_at)
            .all()
        )

    def update(self, loan: Loan) -> Loan:
        self._s.flush()
        return loan

    def count_checkouts_by_month(
        self, *, since: datetime, branch_id: int | None = None
    ) -> list[tuple[int, int, int]]:
        """Checkouts grouped by (year, month). Python-side aggregation to
        dodge sqlite/postgres date-formatter differences; fine at target scale."""
        q = self._s.query(Loan.checked_out_at).filter(Loan.checked_out_at >= since)
        if branch_id is not None:
            q = q.filter(Loan.branch_id == branch_id)
        counts: dict[tuple[int, int], int] = {}
        for (ts,) in q.all():
            key = (ts.year, ts.month)
            counts[key] = counts.get(key, 0) + 1
        return sorted([(y, m, c) for (y, m), c in counts.items()])

    def popular_works(
        self,
        *,
        since: datetime,
        until: datetime,
        limit: int,
        branch_id: int | None = None,
    ) -> list[tuple[int, int]]:
        """Top works by checkout count in [since, until). Returns (work_id, count)."""
        q = (
            self._s.query(Item.work_id, func.count(Loan.id).label("c"))
            .join(Item, Loan.item_id == Item.id)
            .filter(Loan.checked_out_at >= since, Loan.checked_out_at < until)
        )
        if branch_id is not None:
            q = q.filter(Loan.branch_id == branch_id)
        q = (
            q.group_by(Item.work_id)
            .order_by(func.count(Loan.id).desc())
            .limit(limit)
        )
        return [(wid, c) for wid, c in q.all()]

    def list_active_overdue_joined(
        self, *, branch_id: int | None = None
    ) -> list[tuple[Loan, Patron, Item, Work]]:
        """Active overdue loans with patron/item/work eagerly fetched.
        Ordered by due_at ascending (most-overdue first)."""
        now = datetime.now(tz=timezone.utc)
        q = (
            self._s.query(Loan, Patron, Item, Work)
            .join(Patron, Loan.patron_id == Patron.id)
            .join(Item, Loan.item_id == Item.id)
            .join(Work, Item.work_id == Work.id)
            .filter(Loan.returned_at.is_(None), Loan.due_at < now)
        )
        if branch_id is not None:
            q = q.filter(Loan.branch_id == branch_id)
        return q.order_by(Loan.due_at).all()

    # ------------------------------------------------------------------
    # Librarian list views: all-active, patron history, item history
    # ------------------------------------------------------------------

    def _active_filter(
        self,
        *,
        due: str | None,
        branch_id: int | None,
        query: str | None,
    ):
        now = datetime.now(tz=timezone.utc)
        q = self._s.query(Loan).filter(Loan.returned_at.is_(None))
        if branch_id is not None:
            q = q.filter(Loan.branch_id == branch_id)
        if due == "overdue":
            q = q.filter(Loan.due_at < now)
        elif due == "due_soon":
            q = q.filter(Loan.due_at >= now, Loan.due_at <= now + timedelta(days=3))
        elif due == "on_time":
            q = q.filter(Loan.due_at > now + timedelta(days=3))
        if query:
            like = f"%{query}%"
            q = (
                q.join(Patron, Loan.patron_id == Patron.id)
                .join(Item, Loan.item_id == Item.id)
                .join(Work, Item.work_id == Work.id)
                .filter(
                    or_(
                        Patron.full_name.ilike(like),
                        Patron.library_card_number.ilike(like),
                        Item.barcode.ilike(like),
                        Work.title.ilike(like),
                    )
                )
            )
        return q

    def list_active(
        self,
        *,
        due: str | None = None,
        branch_id: int | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Loan]:
        return (
            self._active_filter(due=due, branch_id=branch_id, query=query)
            .options(
                joinedload(Loan.patron),
                joinedload(Loan.item).joinedload(Item.work),
                joinedload(Loan.branch),
            )
            .order_by(Loan.due_at.asc(), Loan.id.asc())
            .limit(limit)
            .offset(offset)
            .all()
        )

    def count_active(
        self,
        *,
        due: str | None = None,
        branch_id: int | None = None,
        query: str | None = None,
    ) -> int:
        return self._active_filter(due=due, branch_id=branch_id, query=query).count()

    def list_for_patron(
        self,
        patron_id: int,
        *,
        status: str = "active",
        limit: int = 100,
        offset: int = 0,
    ) -> list[Loan]:
        """status: 'active' | 'returned' | 'all'. Ordered newest-first."""
        q = self._s.query(Loan).filter(Loan.patron_id == patron_id)
        if status == "active":
            q = q.filter(Loan.returned_at.is_(None))
        elif status == "returned":
            q = q.filter(Loan.returned_at.is_not(None))
        # 'all' — no extra filter
        return (
            q.options(joinedload(Loan.item).joinedload(Item.work))
            .order_by(Loan.checked_out_at.desc(), Loan.id.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

    def count_for_patron(self, patron_id: int, *, status: str = "active") -> int:
        q = self._s.query(Loan).filter(Loan.patron_id == patron_id)
        if status == "active":
            q = q.filter(Loan.returned_at.is_(None))
        elif status == "returned":
            q = q.filter(Loan.returned_at.is_not(None))
        return q.count()

    def list_for_item(
        self, item_id: int, *, limit: int = 25, offset: int = 0
    ) -> list[Loan]:
        """Loan history for a specific copy, newest-first."""
        return (
            self._s.query(Loan)
            .options(joinedload(Loan.patron))
            .filter(Loan.item_id == item_id)
            .order_by(Loan.checked_out_at.desc(), Loan.id.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )
