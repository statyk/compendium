from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from compendium.domain.models import AuditLog


class SqlAuditLogRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, entry: AuditLog) -> None:
        self._s.add(entry)
        self._s.flush()

    def list(
        self,
        entity_type: str | None = None,
        entity_id: int | None = None,
        user_id: int | None = None,
        limit: int = 50,
    ) -> list[AuditLog]:
        q = self._s.query(AuditLog).order_by(AuditLog.occurred_at.desc())
        if entity_type:
            q = q.filter(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            q = q.filter(AuditLog.entity_id == entity_id)
        if user_id is not None:
            q = q.filter(AuditLog.user_id == user_id)
        return q.limit(limit).all()

    def count_older_than(self, cutoff: datetime) -> int:
        return (
            self._s.query(AuditLog)
            .filter(AuditLog.occurred_at < cutoff)
            .count()
        )

    def delete_older_than(self, cutoff: datetime) -> int:
        deleted = (
            self._s.query(AuditLog)
            .filter(AuditLog.occurred_at < cutoff)
            .delete(synchronize_session=False)
        )
        self._s.flush()
        return int(deleted)
