from __future__ import annotations

from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser, Household, Patron
from compendium.repositories.base import HouseholdRepository, LoanRepository, PatronRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService

_MISSING = object()


class HouseholdService:
    def __init__(
        self,
        household_repo: HouseholdRepository,
        patron_repo: PatronRepository,
        loan_repo: LoanRepository,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._households = household_repo
        self._patrons = patron_repo
        self._loans = loan_repo
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

    def create(self, name: str, *, notes: str | None = None) -> Household:
        if not name or not name.strip():
            raise ValidationError("Household name cannot be blank.")
        hh = Household(name=name.strip(), notes=notes)
        result = self._households.add(hh)
        self._record(result.id, AuditAction.CREATE, {"name": name})
        return result

    def get(self, household_id: int) -> Household:
        hh = self._households.get(household_id)
        if hh is None:
            raise NotFoundError(f"Household {household_id} not found.")
        return hh

    def list(self, *, limit: int = 50, offset: int = 0) -> list[Household]:
        return self._households.list(limit=limit, offset=offset)

    def count(self) -> int:
        return self._households.count()

    def update(
        self,
        household_id: int,
        *,
        name: str | object = _MISSING,
        notes: str | None | object = _MISSING,
    ) -> Household:
        hh = self.get(household_id)
        changes: dict = {}
        if name is not _MISSING:
            if not name or not str(name).strip():
                raise ValidationError("Household name cannot be blank.")
            hh.name = str(name).strip()
            changes["name"] = hh.name
        if notes is not _MISSING:
            hh.notes = notes  # type: ignore[assignment]
            changes["notes"] = notes
        result = self._households.update(hh)
        if changes:
            self._record(household_id, AuditAction.UPDATE, changes)
        return result

    def delete(self, household_id: int) -> None:
        hh = self.get(household_id)
        members = self._patrons.list_by_household(household_id)
        if members:
            names = ", ".join(m.library_card_number for m in members[:3])
            extra = f" (and {len(members) - 3} more)" if len(members) > 3 else ""
            raise BusinessRuleError(
                f"Household has {len(members)} members ({names}{extra}). "
                "Remove all members before deleting."
            )
        self._record(household_id, AuditAction.DELETE, {"name": hh.name})
        self._households.delete(hh)

    def get_members(self, household_id: int) -> list[Patron]:
        self.get(household_id)
        return self._patrons.list_by_household(household_id)

    def add_member(self, household_id: int, card_number: str) -> Patron:
        hh = self._households.get(household_id)
        if hh is None:
            raise NotFoundError(f"Household {household_id} not found.")
        patron = self._patrons.get_by_card_number(card_number)
        if patron is None:
            raise NotFoundError(f"Patron with card '{card_number}' not found.")
        if patron.household_id == household_id:
            return patron
        if patron.household_id is not None:
            raise BusinessRuleError(
                f"Patron '{card_number}' is already in a household (id={patron.household_id}). "
                "Remove them from that household first."
            )
        patron.household_id = household_id
        result = self._patrons.update(patron)
        self._record(
            household_id,
            AuditAction.UPDATE,
            {"added_member": card_number, "patron_id": patron.id},
        )
        return result

    def remove_member(self, household_id: int, card_number: str) -> Patron:
        hh = self._households.get(household_id)
        if hh is None:
            raise NotFoundError(f"Household {household_id} not found.")
        patron = self._patrons.get_by_card_number(card_number)
        if patron is None:
            raise NotFoundError(f"Patron with card '{card_number}' not found.")
        if patron.household_id != household_id:
            raise BusinessRuleError(
                f"Patron '{card_number}' is not a member of household {household_id}."
            )
        patron.household_id = None
        result = self._patrons.update(patron)
        self._record(
            household_id,
            AuditAction.UPDATE,
            {"removed_member": card_number, "patron_id": patron.id},
        )
        return result

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
                entity_type=AuditEntityType.HOUSEHOLD,
                entity_id=entity_id,
                action=action,
                details=details,
            )
