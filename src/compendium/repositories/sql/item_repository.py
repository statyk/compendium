from datetime import datetime

from sqlalchemy import func, nulls_first, or_
from sqlalchemy.orm import Session

from compendium.domain.enums import ItemStatus
from compendium.domain.models import Item, Loan, Work


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

    def list_dormant(
        self,
        *,
        not_since: datetime,
        limit: int,
        branch_id: int | None = None,
    ) -> list[tuple[Item, Work, datetime | None]]:
        """Items whose most recent checkout is older than `not_since`, plus
        items never checked out. Excludes withdrawn items. Nulls (never-loaned)
        sort first so the neediest weeding candidates land at the top."""
        last_checkout = (
            self._s.query(
                Loan.item_id.label("item_id"),
                func.max(Loan.checked_out_at).label("last"),
            )
            .group_by(Loan.item_id)
            .subquery()
        )
        q = (
            self._s.query(Item, Work, last_checkout.c.last)
            .join(Work, Item.work_id == Work.id)
            .outerjoin(last_checkout, last_checkout.c.item_id == Item.id)
            .filter(Item.status != ItemStatus.WITHDRAWN.value)
            .filter(or_(last_checkout.c.last.is_(None), last_checkout.c.last < not_since))
        )
        if branch_id is not None:
            q = q.filter(Item.branch_id == branch_id)
        q = q.order_by(nulls_first(last_checkout.c.last.asc()), Item.id).limit(limit)
        return q.all()
