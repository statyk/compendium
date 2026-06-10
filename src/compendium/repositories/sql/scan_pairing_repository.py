from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from compendium.domain.models import ScanPairing


class SqlScanPairingRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, pairing: ScanPairing) -> ScanPairing:
        self._s.add(pairing)
        self._s.flush()
        return pairing

    def get(self, pairing_id: int) -> ScanPairing | None:
        return self._s.get(ScanPairing, pairing_id)

    def get_by_token_hash(self, token_hash: str) -> ScanPairing | None:
        return (
            self._s.query(ScanPairing)
            .filter(ScanPairing.token_hash == token_hash)
            .one_or_none()
        )

    def _terminal_filter(self, cutoff: datetime):
        """Return a SQLAlchemy filter for terminal pairings older than cutoff.

        A pairing is terminal when it can no longer be used: either its
        session window has expired (``expires_at < cutoff``) or it was
        explicitly revoked before the cutoff (``revoked_at IS NOT NULL AND
        revoked_at < cutoff``).  Live, unexpired sessions are never included.
        """
        return or_(
            ScanPairing.expires_at < cutoff,
            (ScanPairing.revoked_at.isnot(None)) & (ScanPairing.revoked_at < cutoff),
        )

    def terminal_deletable_ids(self, cutoff: datetime) -> list[int]:
        """Return IDs of terminal pairings that are safe to delete.

        A pairing is deletable only when it has NO un-resolved
        (``status="pending"``) pending items. Pairings whose pending queue still
        contains unresolved rows are skipped so no patron-visible work is lost.
        """
        from compendium.domain.models import ScanPendingItem

        pending_subq = (
            self._s.query(ScanPendingItem.pairing_id)
            .filter(ScanPendingItem.status == "pending")
            .subquery()
        )
        rows = (
            self._s.query(ScanPairing.id)
            .filter(self._terminal_filter(cutoff))
            .filter(ScanPairing.id.notin_(self._s.query(pending_subq.c.pairing_id)))
            .all()
        )
        return [r[0] for r in rows]

    def delete_by_ids(self, ids: list[int]) -> int:
        """Delete pairings by explicit ID list. Returns row count deleted."""
        if not ids:
            return 0
        deleted = (
            self._s.query(ScanPairing)
            .filter(ScanPairing.id.in_(ids))
            .delete(synchronize_session=False)
        )
        self._s.flush()
        return int(deleted)
