from __future__ import annotations

from sqlalchemy.orm import Session

from compendium.domain.models import PatronCategory


class SqlPatronCategoryRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, category: PatronCategory) -> PatronCategory:
        self._s.add(category)
        self._s.flush()
        return category

    def get(self, category_id: int) -> PatronCategory | None:
        return self._s.get(PatronCategory, category_id)

    def get_by_code(self, code: str) -> PatronCategory | None:
        return self._s.query(PatronCategory).filter_by(code=code).first()

    def get_default(self) -> PatronCategory | None:
        return (
            self._s.query(PatronCategory).filter(PatronCategory.is_default.is_(True)).first()
        )

    def list(self) -> list[PatronCategory]:
        return self._s.query(PatronCategory).order_by(PatronCategory.display_name).all()

    def update(self, category: PatronCategory) -> PatronCategory:
        self._s.flush()
        return category

    def delete(self, category: PatronCategory) -> None:
        self._s.delete(category)
        self._s.flush()

    def clear_defaults(self) -> None:
        self._s.query(PatronCategory).filter(PatronCategory.is_default.is_(True)).update(
            {"is_default": False}, synchronize_session="evaluate"
        )

    def count_patrons_in(self, category_id: int) -> int:
        from compendium.domain.models import Patron

        return self._s.query(Patron).filter(Patron.category_id == category_id).count()

    def count_policies_in(self, category_id: int) -> int:
        from compendium.domain.models import LoanPolicy

        return (
            self._s.query(LoanPolicy)
            .filter(LoanPolicy.patron_category_id == category_id)
            .count()
        )
