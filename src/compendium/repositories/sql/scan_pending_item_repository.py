from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from compendium.domain.models import ScanPairing, ScanPendingItem


class SqlScanPendingItemRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, item: ScanPendingItem) -> ScanPendingItem:
        self._s.add(item)
        self._s.flush()
        return item

    def get(self, pending_id: int) -> ScanPendingItem | None:
        return self._s.get(ScanPendingItem, pending_id)

    def pending_for_user(self, user_id: int) -> list[ScanPendingItem]:
        """Un-resolved pending items across the staff user's pairings.

        Scoped by the owning pairing's ``user_id`` so the desk queue survives
        unpairing (the pairing row persists until pruned).
        """
        return (
            self._s.query(ScanPendingItem)
            .join(ScanPairing, ScanPendingItem.pairing_id == ScanPairing.id)
            .filter(ScanPairing.user_id == user_id)
            .filter(ScanPendingItem.status == "pending")
            .order_by(ScanPendingItem.id.desc())
            .all()
        )

    def delete_for_pairings(self, pairing_ids: list[int]) -> int:
        if not pairing_ids:
            return 0
        deleted = (
            self._s.query(ScanPendingItem)
            .filter(ScanPendingItem.pairing_id.in_(pairing_ids))
            .delete(synchronize_session=False)
        )
        self._s.flush()
        return int(deleted)

    def delete_resolved_older_than(self, cutoff: datetime) -> int:
        deleted = (
            self._s.query(ScanPendingItem)
            .filter(ScanPendingItem.status != "pending")
            .filter(ScanPendingItem.resolved_at.isnot(None))
            .filter(ScanPendingItem.resolved_at < cutoff)
            # "evaluate" (not False) so a same-session get() after the prune
            # returns None instead of a stale identity-map object.
            .delete(synchronize_session="evaluate")
        )
        self._s.flush()
        return int(deleted)
