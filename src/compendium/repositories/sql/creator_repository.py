from sqlalchemy.orm import Session

from compendium.domain.models import Creator


class SqlCreatorRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, creator: Creator) -> Creator:
        self._s.add(creator)
        self._s.flush()
        return creator

    def get_by_sort_name(self, sort_name: str) -> Creator | None:
        return self._s.query(Creator).filter_by(sort_name=sort_name).first()
