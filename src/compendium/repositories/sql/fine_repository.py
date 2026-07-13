from __future__ import annotations

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from compendium.domain.enums import FineKind, FineStatus
from compendium.domain.models import Fine, Patron


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
            self._s.query(
                func.coalesce(func.sum(Fine.amount_cents - Fine.paid_cents), 0)
            )
            .filter(
                Fine.patron_id == patron_id,
                Fine.status == FineStatus.OUTSTANDING.value,
            )
            .scalar()
        )
        return int(total or 0)

    def update(self, fine: Fine) -> Fine:
        self._s.flush()
        return fine

    # ------------------------------------------------------------------
    # Librarian list views: system-wide outstanding fines
    # ------------------------------------------------------------------

    def _outstanding_filter(
        self,
        *,
        kind: str | None,
        query: str | None,
    ):
        q = self._s.query(Fine).filter(Fine.status == FineStatus.OUTSTANDING.value)
        if kind:
            q = q.filter(Fine.kind == kind)
        if query:
            like = f"%{query}%"
            q = q.join(Patron, Fine.patron_id == Patron.id).filter(
                or_(
                    Patron.full_name.ilike(like),
                    Patron.library_card_number.ilike(like),
                )
            )
        return q

    def list_outstanding(
        self,
        *,
        kind: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Fine]:
        return (
            self._outstanding_filter(kind=kind, query=query)
            .options(joinedload(Fine.patron), joinedload(Fine.item))
            .order_by(Fine.assessed_at.desc(), Fine.id.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

    def count_outstanding(
        self,
        *,
        kind: str | None = None,
        query: str | None = None,
    ) -> int:
        return self._outstanding_filter(kind=kind, query=query).count()

    def outstanding_total_all(
        self,
        *,
        kind: str | None = None,
        query: str | None = None,
    ) -> int:
        """Sum of balance (amount_cents - paid_cents) across outstanding fines matching the filter."""
        total = (
            self._outstanding_filter(kind=kind, query=query)
            .with_entities(
                func.coalesce(func.sum(Fine.amount_cents - Fine.paid_cents), 0)
            )
            .scalar()
        )
        return int(total or 0)
