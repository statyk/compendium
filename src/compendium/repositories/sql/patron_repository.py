from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from compendium.domain.models import Patron

_STATUSES = ("active", "inactive", "all")


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

    def get_by_user_id(self, user_id: int) -> Patron | None:
        return self._s.query(Patron).filter_by(user_id=user_id).first()

    def update(self, patron: Patron) -> Patron:
        self._s.flush()
        return patron

    def _filtered(self, *, status: str, query: str | None):
        if status not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}")
        q = self._s.query(Patron)
        if status == "active":
            q = q.filter(Patron.is_active == True)  # noqa: E712
        elif status == "inactive":
            q = q.filter(Patron.is_active == False)  # noqa: E712
        if query:
            like = f"%{query}%"
            q = q.filter(
                or_(
                    Patron.full_name.ilike(like),
                    Patron.library_card_number.ilike(like),
                    Patron.contact_email.ilike(like),
                )
            )
        return q

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        status: str = "active",
        query: str | None = None,
    ) -> list[Patron]:
        return (
            self._filtered(status=status, query=query)
            .order_by(Patron.full_name)
            .offset(offset)
            .limit(limit)
            .all()
        )

    def count(self, *, status: str = "active", query: str | None = None) -> int:
        return self._filtered(status=status, query=query).count()

    def list_by_household(self, household_id: int) -> list[Patron]:
        return (
            self._s.query(Patron)
            .filter(Patron.household_id == household_id)
            .order_by(Patron.full_name)
            .all()
        )
