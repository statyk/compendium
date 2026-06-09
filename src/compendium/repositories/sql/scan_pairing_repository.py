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

    def count_terminal_older_than(self, cutoff: datetime) -> int:
        """Count pairings that are no longer usable and older than cutoff."""
        return (
            self._s.query(ScanPairing)
            .filter(self._terminal_filter(cutoff))
            .count()
        )

    def delete_terminal_older_than(self, cutoff: datetime) -> int:
        """Delete pairings that are no longer usable and older than cutoff.

        Returns the number of rows deleted.
        """
        deleted = (
            self._s.query(ScanPairing)
            .filter(self._terminal_filter(cutoff))
            .delete(synchronize_session=False)
        )
        self._s.flush()
        return int(deleted)
