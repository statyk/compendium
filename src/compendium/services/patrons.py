from __future__ import annotations

from compendium.domain.enums import HoldStatus
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import Patron
from compendium.repositories.base import HoldRepository, LoanRepository, PatronRepository


class PatronService:
    def __init__(
        self,
        patron_repo: PatronRepository,
        loan_repo: LoanRepository,
        hold_repo: HoldRepository,
    ) -> None:
        self._patrons = patron_repo
        self._loans = loan_repo
        self._holds = hold_repo

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
        return self._patrons.update(patron)
