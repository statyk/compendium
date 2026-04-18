from __future__ import annotations

from sqlalchemy.orm import Session

from compendium.domain.models import Work


class SqlWorkRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, work: Work) -> Work:
        self._s.add(work)
        self._s.flush()
        return work

    def get(self, id: int) -> Work | None:
        return self._s.get(Work, id)

    def get_by_isbn(self, isbn: str) -> Work | None:
        return self._s.query(Work).filter_by(isbn=isbn).first()

    def list(self, limit: int = 50, offset: int = 0) -> list[Work]:
        return self._s.query(Work).order_by(Work.title).offset(offset).limit(limit).all()

    def search(self, q: str, limit: int = 20) -> list[Work]:
        pattern = f"%{q}%"
        return (
            self._s.query(Work)
            .filter(Work.title.ilike(pattern))
            .order_by(Work.title)
            .limit(limit)
            .all()
        )
