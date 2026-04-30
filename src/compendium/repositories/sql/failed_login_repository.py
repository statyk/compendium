from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from compendium.domain.models import FailedLogin


class SqlFailedLoginRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def record(self, scope: str, identifier: str, occurred_at: datetime) -> None:
        self._s.add(FailedLogin(scope=scope, identifier=identifier, occurred_at=occurred_at))
        self._s.flush()

    def count_and_oldest(
        self, scope: str, identifier: str, since: datetime
    ) -> tuple[int, datetime | None]:
        """Return (count, oldest_occurred_at) for failures within the window."""
        rows = (
            self._s.query(FailedLogin.occurred_at)
            .filter(
                FailedLogin.scope == scope,
                FailedLogin.identifier == identifier,
                FailedLogin.occurred_at >= since,
            )
            .all()
        )
        if not rows:
            return 0, None
        timestamps = [r[0] for r in rows]
        return len(timestamps), min(timestamps)

    def clear(self, scope: str, identifier: str) -> None:
        self._s.query(FailedLogin).filter(
            FailedLogin.scope == scope,
            FailedLogin.identifier == identifier,
        ).delete(synchronize_session=False)
        self._s.flush()

    def count_older_than(self, cutoff: datetime) -> int:
        return (
            self._s.query(FailedLogin)
            .filter(FailedLogin.occurred_at < cutoff)
            .count()
        )

    def delete_older_than(self, cutoff: datetime) -> int:
        deleted = (
            self._s.query(FailedLogin)
            .filter(FailedLogin.occurred_at < cutoff)
            .delete(synchronize_session=False)
        )
        self._s.flush()
        return int(deleted)
