from __future__ import annotations

from sqlalchemy.orm import Session

from compendium.domain.models import ItemNote


class SqlItemNoteRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, note: ItemNote) -> ItemNote:
        self._s.add(note)
        self._s.flush()
        return note

    def get(self, note_id: int) -> ItemNote | None:
        return self._s.get(ItemNote, note_id)

    def list_for_item(self, item_id: int) -> list[ItemNote]:
        return (
            self._s.query(ItemNote)
            .filter(ItemNote.item_id == item_id)
            .order_by(ItemNote.created_at.desc())
            .all()
        )

    def delete(self, note: ItemNote) -> None:
        self._s.delete(note)
        self._s.flush()
