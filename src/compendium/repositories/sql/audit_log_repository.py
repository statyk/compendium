from __future__ import annotations

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
