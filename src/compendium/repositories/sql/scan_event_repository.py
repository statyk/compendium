from __future__ import annotations

from sqlalchemy.orm import Session

from compendium.domain.models import ScanEvent


class SqlScanEventRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, event: ScanEvent) -> ScanEvent:
        self._s.add(event)
        self._s.flush()
        return event

    def recent_for_pairing(self, pairing_id: int, limit: int = 25) -> list[ScanEvent]:
        return (
            self._s.query(ScanEvent)
            .filter(ScanEvent.pairing_id == pairing_id)
            .order_by(ScanEvent.id.desc())
            .limit(limit)
            .all()
        )

    def delete_for_pairings(self, pairing_ids: list[int]) -> int:
        if not pairing_ids:
            return 0
        deleted = (
            self._s.query(ScanEvent)
            .filter(ScanEvent.pairing_id.in_(pairing_ids))
            .delete(synchronize_session=False)
        )
        self._s.flush()
        return int(deleted)
