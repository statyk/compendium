from __future__ import annotations

from compendium.domain.models import AppUser, AuditLog
from compendium.repositories.base import AuditLogRepository


class AuditEntityType:
    WORK = "work"
    ITEM = "item"
    PATRON = "patron"
    USER = "user"
    POLICY = "policy"
    ROLE = "role"
    CREATOR = "creator"


class AuditAction:
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    WITHDRAW = "withdraw"
    DEACTIVATE = "deactivate"
    REACTIVATE = "reactivate"
    SET_LOANABLE = "set_loanable"
    BULK_IMPORT = "bulk_import"


class AuditService:
    def __init__(self, repo: AuditLogRepository) -> None:
        self._repo = repo

    def record(
        self,
        *,
        actor: AppUser | None,
        actor_label: str | None,
        source: str,
        entity_type: str,
        entity_id: int | None,
        action: str,
        details: dict | None = None,
    ) -> None:
        resolved_label = actor_label if actor_label else (actor.username if actor else None)
        entry = AuditLog(
            user_id=actor.id if actor else None,
            actor_label=resolved_label,
            source=source,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            details=details or {},
        )
        self._repo.add(entry)

    def list(
        self,
        entity_type: str | None = None,
        entity_id: int | None = None,
        user_id: int | None = None,
        limit: int = 50,
    ) -> list[AuditLog]:
        return self._repo.list(
            entity_type=entity_type,
            entity_id=entity_id,
            user_id=user_id,
            limit=limit,
        )
