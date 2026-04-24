from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from compendium.domain.models import SiteSetting


class SqlSiteSettingRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, key: str) -> SiteSetting | None:
        return self._s.get(SiteSetting, key)

    def all(self) -> list[SiteSetting]:
        return self._s.query(SiteSetting).all()

    def max_updated_at(self) -> datetime | None:
        return self._s.scalar(select(func.max(SiteSetting.updated_at)))

    def upsert(
        self,
        key: str,
        value: str,
        *,
        updated_by_id: int | None = None,
    ) -> SiteSetting:
        row = self.get(key)
        if row is None:
            row = SiteSetting(key=key, value=value, updated_by_id=updated_by_id)
            self._s.add(row)
        else:
            row.value = value
            row.updated_by_id = updated_by_id
        self._s.flush()
        return row

    def delete(self, key: str) -> bool:
        row = self.get(key)
        if row is None:
            return False
        self._s.delete(row)
        self._s.flush()
        return True
