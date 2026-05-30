from __future__ import annotations

from sqlalchemy.orm import Session

from compendium.domain.models import CuratedList, CuratedListEntry


class SqlCuratedListRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, curated_list: CuratedList) -> CuratedList:
        self._s.add(curated_list)
        self._s.flush()
        return curated_list

    def get(self, list_id: int) -> CuratedList | None:
        return self._s.get(CuratedList, list_id)

    def get_by_slug(self, slug: str) -> CuratedList | None:
        return self._s.query(CuratedList).filter_by(slug=slug).first()

    def slug_exists(self, slug: str) -> bool:
        return self._s.query(CuratedList).filter_by(slug=slug).count() > 0

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        public_only: bool = False,
        featured_only: bool = False,
    ) -> list[CuratedList]:
        q = self._s.query(CuratedList)
        if public_only:
            q = q.filter(CuratedList.is_public.is_(True))
        if featured_only:
            q = q.filter(CuratedList.is_featured.is_(True))
        return q.order_by(CuratedList.display_order, CuratedList.name).offset(offset).limit(limit).all()

    def count(self, *, public_only: bool = False, featured_only: bool = False) -> int:
        q = self._s.query(CuratedList)
        if public_only:
            q = q.filter(CuratedList.is_public.is_(True))
        if featured_only:
            q = q.filter(CuratedList.is_featured.is_(True))
        return q.count()

    def update(self, curated_list: CuratedList) -> CuratedList:
        self._s.flush()
        return curated_list

    def delete(self, curated_list: CuratedList) -> None:
        self._s.delete(curated_list)
        self._s.flush()

    def add_entry(self, entry: CuratedListEntry) -> CuratedListEntry:
        self._s.add(entry)
        self._s.flush()
        return entry

    def remove_entry(self, list_id: int, work_id: int) -> None:
        entry = self.get_entry(list_id, work_id)
        if entry:
            self._s.delete(entry)
            self._s.flush()

    def get_entry(self, list_id: int, work_id: int) -> CuratedListEntry | None:
        return (
            self._s.query(CuratedListEntry)
            .filter_by(list_id=list_id, work_id=work_id)
            .first()
        )

    def update_entry(self, entry: CuratedListEntry) -> CuratedListEntry:
        self._s.flush()
        return entry

    def max_entry_order(self, list_id: int) -> int:
        result = (
            self._s.query(CuratedListEntry.display_order)
            .filter_by(list_id=list_id)
            .order_by(CuratedListEntry.display_order.desc())
            .first()
        )
        return result[0] if result else -1
