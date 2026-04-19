from __future__ import annotations

from sqlalchemy.orm import Session

from compendium.domain.models import MediaType


class SqlMediaTypeRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_code(self, code: str) -> MediaType | None:
        return self._s.query(MediaType).filter_by(code=code).first()

    def list(self) -> list[MediaType]:
        return self._s.query(MediaType).order_by(MediaType.display_name).all()
