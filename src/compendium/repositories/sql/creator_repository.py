from sqlalchemy import exists
from sqlalchemy.orm import Session

from compendium.domain.enums import ItemStatus
from compendium.domain.models import Creator, Item, Work, WorkCreator


class SqlCreatorRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, creator: Creator) -> Creator:
        self._s.add(creator)
        self._s.flush()
        return creator

    def get(self, id: int) -> Creator | None:
        return self._s.get(Creator, id)

    def get_by_sort_name(self, sort_name: str) -> Creator | None:
        return self._s.query(Creator).filter_by(sort_name=sort_name).first()

    def update(self, creator: Creator) -> Creator:
        self._s.flush()
        return creator

    def list_works(self, creator_id: int, *, include_withdrawn_only: bool = False) -> list[Work]:
        q = (
            self._s.query(Work)
            .join(WorkCreator, WorkCreator.work_id == Work.id)
            .filter(WorkCreator.creator_id == creator_id)
            .distinct()
        )
        if not include_withdrawn_only:
            q = q.filter(
                exists().where(
                    (Item.work_id == Work.id)
                    & (Item.status != ItemStatus.WITHDRAWN.value)
                )
            )
        return q.all()
