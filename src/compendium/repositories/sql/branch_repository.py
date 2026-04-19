from sqlalchemy.orm import Session

from compendium.domain.models import Branch


class SqlBranchRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_default(self) -> Branch | None:
        return self._s.query(Branch).filter_by(is_default=True).first()

    def get(self, id: int) -> Branch | None:
        return self._s.get(Branch, id)

    def get_by_code(self, code: str) -> Branch | None:
        return self._s.query(Branch).filter_by(code=code).first()

    def list(self) -> list[Branch]:
        return self._s.query(Branch).order_by(Branch.name).all()

    def update(self, branch: Branch) -> Branch:
        self._s.flush()
        return branch
