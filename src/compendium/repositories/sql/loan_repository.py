from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from compendium.domain.models import Loan


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
