from __future__ import annotations

from datetime import date

from compendium.domain.enums import ItemNoteKind
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser, ItemNote
from compendium.repositories.base import ItemNoteRepository, ItemRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService


class ItemNoteService:
    def __init__(
        self,
        item_note_repo: ItemNoteRepository,
        item_repo: ItemRepository,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._item_note_repo = item_note_repo
        self._item_repo = item_repo
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

    def add_note(
        self,
        barcode: str,
        *,
        kind: str,
        note: str,
        event_date: date | None = None,
    ) -> ItemNote:
        item = self._item_repo.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"Item with barcode '{barcode}' not found.")
        if not note or not note.strip():
            raise ValidationError("Note cannot be blank.")
        if kind == ItemNoteKind.STATUS.value:
            raise ValidationError("The 'status' note kind is system-only and cannot be added manually.")
        if kind not in [k.value for k in ItemNoteKind]:
            raise ValidationError(f"Invalid note kind: '{kind}'.")
        new_note = ItemNote(
            item_id=item.id,
            kind=kind,
            note=note.strip(),
            event_date=event_date,
            is_system=False,
            user_id=self._actor.id if self._actor else None,
            actor_label=self._actor_label,
        )
        result = self._item_note_repo.add(new_note)
        self._record(item.id, AuditAction.NOTE_ADD, {"barcode": barcode, "kind": kind})
        return result

    def list_for_item(self, barcode: str) -> list[ItemNote]:
        item = self._item_repo.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"Item with barcode '{barcode}' not found.")
        return self._item_note_repo.list_for_item(item.id)

    def delete_note(self, barcode: str, note_id: int) -> None:
        item = self._item_repo.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"Item with barcode '{barcode}' not found.")
        note = self._item_note_repo.get(note_id)
        if note is None:
            raise NotFoundError(f"Note {note_id} not found.")
        if note.item_id != item.id:
            raise NotFoundError(f"Note {note_id} does not belong to item '{barcode}'.")
        if note.is_system:
            raise BusinessRuleError("System-generated history entries cannot be deleted.")
        self._item_note_repo.delete(note)
        self._record(item.id, AuditAction.NOTE_DELETE, {"barcode": barcode, "note_id": note_id})

    def _record(
        self,
        entity_id: int | None,
        action: str,
        details: dict | None = None,
    ) -> None:
        if self._audit is not None:
            self._audit.record(
                actor=self._actor,
                actor_label=self._actor_label,
                source=self._source,
                entity_type=AuditEntityType.ITEM,
                entity_id=entity_id,
                action=action,
                details=details,
            )


def record_system_note(repo: ItemNoteRepository | None, item_id: int, kind: str, text: str) -> None:
    """Append a system-generated note; no-op if repo is None."""
    if repo is not None:
        from compendium.domain.models import ItemNote
        repo.add(ItemNote(item_id=item_id, kind=kind, note=text, is_system=True))
