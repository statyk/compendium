from __future__ import annotations

from sqlalchemy.orm import Session

from compendium.domain.models import Household


class SqlHouseholdRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, household: Household) -> Household:
        self._s.add(household)
        self._s.flush()
        return household

    def get(self, household_id: int) -> Household | None:
        return self._s.get(Household, household_id)

    def list(self, *, limit: int = 50, offset: int = 0) -> list[Household]:
        return (
            self._s.query(Household)
            .order_by(Household.name)
            .offset(offset)
            .limit(limit)
            .all()
        )

    def count(self) -> int:
        return self._s.query(Household).count()

    def update(self, household: Household) -> Household:
        self._s.flush()
        return household

    def delete(self, household: Household) -> None:
        self._s.delete(household)
        self._s.flush()
