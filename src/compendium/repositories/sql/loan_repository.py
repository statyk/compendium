from sqlalchemy.orm import Session

from compendium.domain.models import Loan


class SqlLoanRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, loan: Loan) -> Loan:
        self._s.add(loan)
        self._s.flush()
        return loan

    def get_active_for_item(self, item_id: int) -> Loan | None:
        return (
            self._s.query(Loan)
            .filter(Loan.item_id == item_id, Loan.returned_at.is_(None))
            .first()
        )

    def get_active_for_patron(self, patron_id: int) -> list[Loan]:
        return (
            self._s.query(Loan)
            .filter(Loan.patron_id == patron_id, Loan.returned_at.is_(None))
            .all()
        )

    def update(self, loan: Loan) -> Loan:
        self._s.flush()
        return loan
