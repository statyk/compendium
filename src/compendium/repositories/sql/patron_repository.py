from sqlalchemy.orm import Session

from compendium.domain.models import Patron


class SqlPatronRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, patron: Patron) -> Patron:
        self._s.add(patron)
        self._s.flush()
        return patron

    def get(self, id: int) -> Patron | None:
        return self._s.get(Patron, id)

    def get_by_card_number(self, card_number: str) -> Patron | None:
        return self._s.query(Patron).filter_by(library_card_number=card_number).first()

    def list(self, limit: int = 50, offset: int = 0) -> list[Patron]:
        return self._s.query(Patron).order_by(Patron.full_name).offset(offset).limit(limit).all()
