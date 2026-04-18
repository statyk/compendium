from sqlalchemy.orm import Session

from compendium.domain.models import Item


class SqlItemRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, item: Item) -> Item:
        self._s.add(item)
        self._s.flush()
        return item

    def get(self, id: int) -> Item | None:
        return self._s.get(Item, id)

    def get_by_barcode(self, barcode: str) -> Item | None:
        return self._s.query(Item).filter_by(barcode=barcode).first()

    def update(self, item: Item) -> Item:
        self._s.flush()
        return item

    def count_all(self) -> int:
        return self._s.query(Item).count()
