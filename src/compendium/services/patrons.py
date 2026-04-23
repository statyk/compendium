from __future__ import annotations

import random
from datetime import date

from compendium.domain.enums import HoldStatus
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser, Patron
from compendium.repositories.base import HoldRepository, LoanRepository, PatronRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService

_MISSING = object()


class PatronService:
    def __init__(
        self,
        patron_repo: PatronRepository,
        loan_repo: LoanRepository,
        hold_repo: HoldRepository,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._patrons = patron_repo
        self._loans = loan_repo
        self._holds = hold_repo
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

    def create(
        self,
        full_name: str,
        contact_email: str | None = None,
        contact_phone: str | None = None,
        user_id: int | None = None,
        category_id: int | None = None,
        expires_at: date | None = None,
    ) -> Patron:
        if user_id is not None:
            existing = self._patrons.get_by_user_id(user_id)
            if existing is not None:
                raise BusinessRuleError("This user account is already linked to another patron.")
        for _ in range(10):
            card = f"{random.randint(0, 99_999_999):08d}"
            if self._patrons.get_by_card_number(card) is None:
                break
        patron = Patron(
            library_card_number=card,
            full_name=full_name,
            contact_email=contact_email,
            contact_phone=contact_phone,
            user_id=user_id,
            category_id=category_id,
            expires_at=expires_at,
        )
        self._patrons.add(patron)
        self._record(
            AuditEntityType.PATRON,
            patron.id,
            AuditAction.CREATE,
            {"snapshot": {"name": patron.full_name, "card": patron.library_card_number}},
        )
        return patron

    def update(
        self,
        card_number: str,
        *,
        category_id: int | None | object = _MISSING,
        expires_at: date | None | object = _MISSING,
    ) -> Patron:
        """Edit category and/or expiry. None clears the field; _MISSING leaves it."""
        patron = self._patrons.get_by_card_number(card_number)
        if patron is None:
            raise NotFoundError(f"No patron with card number '{card_number}'")
        before = {"category_id": patron.category_id, "expires_at": patron.expires_at.isoformat() if patron.expires_at else None}
        if category_id is not _MISSING:
            patron.category_id = category_id  # type: ignore[assignment]
        if expires_at is not _MISSING:
            patron.expires_at = expires_at  # type: ignore[assignment]
        result = self._patrons.update(patron)
        self._record(
            AuditEntityType.PATRON,
            patron.id,
            AuditAction.UPDATE,
            {
                "before": before,
                "after": {
                    "category_id": patron.category_id,
                    "expires_at": patron.expires_at.isoformat() if patron.expires_at else None,
                },
            },
        )
        return result

    def deactivate_expired(self, *, today: date | None = None, dry_run: bool = False) -> list[Patron]:
        """Find active patrons whose expires_at < today; flip them to inactive
        unless dry_run. Returns the list of patrons (matched, regardless of dry_run)."""
        cutoff = today or date.today()
        matches = [
            p
            for p in self._patrons.list(limit=1_000_000, offset=0)
            if p.is_active and p.expires_at is not None and p.expires_at < cutoff
        ]
        if dry_run:
            return matches
        for patron in matches:
            patron.is_active = False
            self._patrons.update(patron)
            self._record(
                AuditEntityType.PATRON,
                patron.id,
                AuditAction.DEACTIVATE,
                {
                    "snapshot": {
                        "name": patron.full_name,
                        "card": patron.library_card_number,
                        "expires_at": patron.expires_at.isoformat() if patron.expires_at else None,
                    },
                    "reason": "expired",
                },
            )
        return matches

    def link_user(self, card_number: str, user_id: int) -> Patron:
        patron = self._patrons.get_by_card_number(card_number)
        if patron is None:
            raise NotFoundError(f"No patron with card number '{card_number}'")
        existing = self._patrons.get_by_user_id(user_id)
        if existing is not None and existing.id != patron.id:
            raise BusinessRuleError("This user account is already linked to another patron.")
        patron.user_id = user_id
        result = self._patrons.update(patron)
        self._record(
            AuditEntityType.PATRON,
            patron.id,
            AuditAction.UPDATE,
            {"snapshot": {"card": patron.library_card_number, "linked_user_id": user_id}},
        )
        return result

    def unlink_user(self, card_number: str) -> Patron:
        patron = self._patrons.get_by_card_number(card_number)
        if patron is None:
            raise NotFoundError(f"No patron with card number '{card_number}'")
        if patron.user_id is None:
            raise BusinessRuleError("This patron has no linked user account.")
        patron.user_id = None
        result = self._patrons.update(patron)
        self._record(
            AuditEntityType.PATRON,
            patron.id,
            AuditAction.UPDATE,
            {"snapshot": {"card": patron.library_card_number, "linked_user_id": None}},
        )
        return result

    def deactivate(self, card_number: str) -> Patron:
        patron = self._patrons.get_by_card_number(card_number)
        if patron is None:
            raise NotFoundError(f"No patron with card number '{card_number}'")
        if not patron.is_active:
            raise BusinessRuleError(f"Patron '{card_number}' is already inactive")

        active_loans = self._loans.get_active_for_patron(patron.id)
        if active_loans:
            raise BusinessRuleError(
                f"Patron has {len(active_loans)} active loan(s). Check them in before deactivating."
            )

        for hold in self._holds.get_active_for_patron(patron.id):
            hold.status = HoldStatus.CANCELLED.value
            self._holds.update(hold)

        patron.is_active = False
        result = self._patrons.update(patron)
        self._record(
            AuditEntityType.PATRON,
            patron.id,
            AuditAction.DEACTIVATE,
            {"snapshot": {"name": patron.full_name, "card": patron.library_card_number}},
        )
        return result

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
