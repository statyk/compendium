from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from compendium.domain.enums import HoldStatus
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser, DeletedEntity, Work
from compendium.repositories.base import HoldRepository, TrashRepository, WorkRepository
from compendium.repositories.sql.trash_repository import PAYLOAD_VERSION
from compendium.services.audit import AuditAction, AuditEntityType, AuditService

ENTITY_WORK = "work"


@dataclass(frozen=True)
class DeletedWorkSummary:
    trash_id: int
    original_work_id: int
    label: str
    item_count: int
    deleted_at: datetime


def _summary(row: DeletedEntity) -> DeletedWorkSummary:
    return DeletedWorkSummary(
        trash_id=row.id,
        original_work_id=row.entity_id,
        label=row.label,
        item_count=len(row.payload.get("items", [])),
        deleted_at=row.deleted_at,
    )


class TrashService:
    """Recoverable deletion: snapshot a work graph to deleted_entity, then hard-delete."""

    def __init__(
        self,
        trash_repo: TrashRepository,
        work_repo: WorkRepository,
        hold_repo: HoldRepository,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._trash = trash_repo
        self._works = work_repo
        self._holds = hold_repo
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

    def delete_work(self, work_id: int) -> DeletedWorkSummary:
        work = self._works.get(work_id)
        if work is None:
            raise NotFoundError(f"No work with id={work_id}")
        active = self._trash.count_active_loans(work_id)
        if active:
            raise BusinessRuleError(
                f"Cannot delete '{work.title}': {active} of its copies are on "
                "active loan. Check in all copies first."
            )
        outstanding = self._trash.count_outstanding_fines(work_id)
        if outstanding:
            raise BusinessRuleError(
                f"Cannot delete '{work.title}': {outstanding} outstanding fine(s) "
                "reference its copies. Collect or waive them first."
            )
        cancelled = self._cancel_holds(work_id)
        payload = self._trash.snapshot_work_graph(work)
        item_count = len(payload["items"])
        label = f"{work.title} — {item_count} {'copy' if item_count == 1 else 'copies'}"
        row = self._trash.add(
            DeletedEntity(
                entity_type=ENTITY_WORK,
                entity_id=work.id,
                label=label,
                payload=payload,
                deleted_by=self._actor.id if self._actor else None,
            )
        )
        self._trash.delete_work_graph(work)
        self._record(
            AuditEntityType.WORK,
            row.entity_id,
            AuditAction.DELETE,
            {
                "trash_id": row.id,
                "label": label,
                "item_count": item_count,
                "loan_count": len(payload["loans"]),
                "cancelled_hold_ids": cancelled,
            },
        )
        return _summary(row)

    def restore_work(self, trash_id: int) -> Work:
        row = self._trash.get(trash_id)
        if row is None or row.entity_type != ENTITY_WORK:
            raise NotFoundError(f"No deleted work with trash id={trash_id}")
        payload = row.payload
        if payload.get("version") != PAYLOAD_VERSION:
            raise BusinessRuleError(
                f"Snapshot version {payload.get('version')!r} is not supported; "
                "it was written by a newer Compendium."
            )
        conflicts = self._trash.find_restore_collisions(payload)
        if conflicts:
            raise BusinessRuleError(
                "Cannot restore: "
                + ", ".join(conflicts)
                + " already in use in the live catalog. Resolve the conflicts "
                "and try again."
            )
        work = self._trash.restore_work_graph(payload)
        self._record(
            AuditEntityType.WORK,
            work.id,
            AuditAction.RESTORE,
            {
                "trash_id": trash_id,
                "original_work_id": row.entity_id,
                "new_work_id": work.id,
                "label": row.label,
            },
        )
        self._trash.delete(row)
        return work

    def list_deleted_works(self, limit: int = 50) -> list[DeletedWorkSummary]:
        return [_summary(r) for r in self._trash.list(entity_type=ENTITY_WORK, limit=limit)]

    def purge(
        self,
        *,
        older_than_days: int | None = None,
        trash_id: int | None = None,
    ) -> int:
        if (older_than_days is None) == (trash_id is None):
            raise ValidationError("Pass exactly one of older_than_days or trash_id.")
        if trash_id is not None:
            row = self._trash.get(trash_id)
            if row is None:
                raise NotFoundError(f"No trash entry with id={trash_id}")
            self._trash.delete(row)
            purged = 1
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
            purged = self._trash.delete_older_than(ENTITY_WORK, cutoff)
        if purged:
            self._record(
                AuditEntityType.TRASH,
                trash_id,
                AuditAction.PURGE_TRASH,
                {"purged": purged, "older_than_days": older_than_days},
            )
        return purged

    def _cancel_holds(self, work_id: int) -> list[int]:
        """Cancel every non-terminal hold on the work; mirrors
        CatalogService._cancel_work_holds. Returns the cancelled hold ids."""
        cancelled: list[int] = []
        for hold in self._holds.get_active_for_work(work_id):
            hold.status = HoldStatus.CANCELLED.value
            self._holds.update(hold)
            cancelled.append(hold.id)
        return cancelled

    def _record(
        self,
        entity_type: str,
        entity_id: int | None,
        action: str,
        details: dict | None = None,
    ) -> None:
        if self._audit is not None:
            self._audit.record(
                actor=self._actor,
                actor_label=self._actor_label,
                source=self._source,
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                details=details,
            )
