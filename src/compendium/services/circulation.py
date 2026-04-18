from datetime import datetime, timedelta

from compendium.domain.enums import ItemStatus
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import Loan
from compendium.repositories.base import BranchRepository, ItemRepository, LoanRepository, PatronRepository


class CirculationService:
    def __init__(
        self,
        item_repo: ItemRepository,
        loan_repo: LoanRepository,
        patron_repo: PatronRepository,
        branch_repo: BranchRepository,
        loan_period_days: int = 14,
    ) -> None:
        self._items = item_repo
        self._loans = loan_repo
        self._patrons = patron_repo
        self._branches = branch_repo
        self._loan_period_days = loan_period_days

    def checkout(self, barcode: str, card_number: str) -> Loan:
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")
        if item.status != ItemStatus.AVAILABLE:
            raise BusinessRuleError(
                f"Item '{barcode}' is not available (current status: {item.status})"
            )

        patron = self._patrons.get_by_card_number(card_number)
        if patron is None:
            raise NotFoundError(f"No patron with card number '{card_number}'")
        if not patron.is_active:
            raise BusinessRuleError(f"Patron card '{card_number}' is not active")

        branch = self._branches.get_default()
        now = datetime.utcnow()
        loan = Loan(
            item_id=item.id,
            patron_id=patron.id,
            branch_id=branch.id,  # type: ignore[union-attr]
            checked_out_at=now,
            due_at=now + timedelta(days=self._loan_period_days),
        )
        self._loans.add(loan)

        item.status = ItemStatus.CHECKED_OUT
        self._items.update(item)

        return loan

    def checkin(self, barcode: str) -> Loan:
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")

        loan = self._loans.get_active_for_item(item.id)
        if loan is None:
            raise BusinessRuleError(f"Item '{barcode}' has no active loan to check in")

        loan.returned_at = datetime.utcnow()
        self._loans.update(loan)

        item.status = ItemStatus.AVAILABLE
        self._items.update(item)

        return loan
