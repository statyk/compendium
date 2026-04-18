from sqlalchemy.orm import Session

from compendium.domain.models import Branch


class SqlBranchRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_default(self) -> Branch | None:
        return self._s.query(Branch).filter_by(is_default=True).first()

    def get(self, id: int) -> Branch | None:
        return self._s.get(Branch, id)
