from __future__ import annotations

from sqlalchemy.orm import Session

from compendium.domain.models import LoanPolicy


class SqlLoanPolicyRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, policy: LoanPolicy) -> LoanPolicy:
        self._s.add(policy)
        self._s.flush()
        return policy

    def get(self, policy_id: int) -> LoanPolicy | None:
        return self._s.get(LoanPolicy, policy_id)

    def get_for_media_type(self, media_type_id: int) -> LoanPolicy | None:
        return self._s.query(LoanPolicy).filter(LoanPolicy.media_type_id == media_type_id).first()

    def get_default(self) -> LoanPolicy | None:
        return self._s.query(LoanPolicy).filter(LoanPolicy.is_default.is_(True)).first()

    def list(self) -> list[LoanPolicy]:
        return self._s.query(LoanPolicy).order_by(LoanPolicy.id).all()

    def update(self, policy: LoanPolicy) -> LoanPolicy:
        self._s.flush()
        return policy

    def clear_defaults(self) -> None:
        self._s.query(LoanPolicy).filter(LoanPolicy.is_default.is_(True)).update(
            {"is_default": False}, synchronize_session="evaluate"
        )
