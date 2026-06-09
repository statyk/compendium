from __future__ import annotations

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
