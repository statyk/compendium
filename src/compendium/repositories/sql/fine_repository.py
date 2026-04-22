from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from compendium.domain.enums import FineKind, FineStatus
from compendium.domain.models import Fine, Loan


class SqlFineRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, fine: Fine) -> Fine:
        self._s.add(fine)
        self._s.flush()
        return fine

    def get(self, fine_id: int) -> Fine | None:
        return self._s.get(Fine, fine_id)

    def list(
        self,
        *,
        patron_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Fine]:
        q = self._s.query(Fine)
        if patron_id is not None:
            q = q.filter(Fine.patron_id == patron_id)
        if status is not None:
            q = q.filter(Fine.status == status)
        return q.order_by(Fine.assessed_at.desc()).limit(limit).all()

    def get_outstanding_overdue_for_loan(self, loan_id: int) -> Fine | None:
        return (
            self._s.query(Fine)
            .filter(
                Fine.loan_id == loan_id,
                Fine.kind == FineKind.OVERDUE.value,
                Fine.status == FineStatus.OUTSTANDING.value,
            )
            .first()
        )

    def outstanding_total(self, patron_id: int) -> int:
        total = (
            self._s.query(func.coalesce(func.sum(Fine.amount_cents), 0))
            .filter(
                Fine.patron_id == patron_id,
                Fine.status == FineStatus.OUTSTANDING.value,
            )
            .scalar()
        )
        return int(total or 0)

    def list_active_overdue_loans(
        self, *, patron_id: int | None = None
    ) -> list[Loan]:
        """Return active loans (returned_at IS NULL) whose due_at has passed."""
        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc)
        q = self._s.query(Loan).filter(
            Loan.returned_at.is_(None),
            Loan.due_at < now,
        )
        if patron_id is not None:
            q = q.filter(Loan.patron_id == patron_id)
        return q.order_by(Loan.due_at).all()

    def update(self, fine: Fine) -> Fine:
        self._s.flush()
        return fine
