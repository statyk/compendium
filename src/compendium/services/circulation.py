from __future__ import annotations

from datetime import datetime, timedelta, timezone

from compendium.domain.enums import HoldStatus, ItemStatus
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import Hold, Item, Loan
from compendium.repositories.base import (
    BranchRepository,
    HoldRepository,
    ItemRepository,
    LoanPolicyRepository,
    LoanRepository,
    PatronRepository,
)

_DEFAULT_LOAN_DAYS = 14
_DEFAULT_MAX_RENEWALS = 2


class CirculationService:
    def __init__(
        self,
        item_repo: ItemRepository,
        loan_repo: LoanRepository,
        patron_repo: PatronRepository,
        branch_repo: BranchRepository,
        hold_repo: HoldRepository,
        policy_repo: LoanPolicyRepository,
        hold_pickup_days: int = 3,
    ) -> None:
        self._items = item_repo
        self._loans = loan_repo
        self._patrons = patron_repo
        self._branches = branch_repo
        self._holds = hold_repo
        self._policies = policy_repo
        self._pickup_days = hold_pickup_days

    def _get_policy(self, item: Item) -> tuple[int, int]:
        """Return (loan_period_days, max_renewals) for the item's media type."""
        policy = self._policies.get_for_media_type(item.work.media_type_id)
        if policy is None:
            policy = self._policies.get_default()
        if policy is None:
            return _DEFAULT_LOAN_DAYS, _DEFAULT_MAX_RENEWALS
        return policy.loan_period_days, policy.max_renewals

    def _promote_hold(self, item: Item) -> None:
        """After checkin: promote oldest WAITING hold to AVAILABLE, or free the item."""
        hold = self._holds.get_oldest_waiting_for_work(item.work_id)
        if hold is not None:
            now = datetime.now(timezone.utc)
            hold.status = HoldStatus.AVAILABLE.value
            hold.expires_at = now + timedelta(days=self._pickup_days)
            hold.notified_at = now
            self._holds.update(hold)
            item.status = ItemStatus.ON_HOLD
        else:
            item.status = ItemStatus.AVAILABLE

    def checkout(self, barcode: str, card_number: str) -> Loan:
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")

        patron = self._patrons.get_by_card_number(card_number)
        if patron is None:
            raise NotFoundError(f"No patron with card number '{card_number}'")
        if not patron.is_active:
            raise BusinessRuleError(f"Patron card '{card_number}' is not active")

        fulfilled_hold: Hold | None = None
        if item.status == ItemStatus.ON_HOLD:
            hold = self._holds.get_available_for_patron_work(patron.id, item.work_id)
            if hold is None:
                raise BusinessRuleError(f"Item '{barcode}' is reserved for another patron")
            fulfilled_hold = hold
        elif item.status != ItemStatus.AVAILABLE:
            raise BusinessRuleError(
                f"Item '{barcode}' is not available (current status: {item.status})"
            )

        loan_period_days, _ = self._get_policy(item)
        branch = self._branches.get_default()
        now = datetime.now(timezone.utc)
        loan = Loan(
            item_id=item.id,
            patron_id=patron.id,
            branch_id=branch.id,  # type: ignore[union-attr]
            checked_out_at=now,
            due_at=now + timedelta(days=loan_period_days),
        )
        self._loans.add(loan)

        if fulfilled_hold is not None:
            fulfilled_hold.status = HoldStatus.FULFILLED.value
            self._holds.update(fulfilled_hold)

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

        loan.returned_at = datetime.now(timezone.utc)
        self._loans.update(loan)

        self._promote_hold(item)
        self._items.update(item)
        return loan

    def checkin_by_id(self, loan_id: int) -> Loan:
        loan = self._loans.get(loan_id)
        if loan is None:
            raise NotFoundError(f"No loan with id={loan_id}")
        if loan.returned_at is not None:
            raise BusinessRuleError(f"Loan {loan_id} has already been returned")

        item = self._items.get(loan.item_id)
        if item is None:
            raise NotFoundError(f"No item with id={loan.item_id}")

        loan.returned_at = datetime.now(timezone.utc)
        self._loans.update(loan)

        self._promote_hold(item)
        self._items.update(item)
        return loan

    def renew(self, barcode: str, card_number: str) -> Loan:
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")

        loan = self._loans.get_active_for_item(item.id)
        if loan is None:
            raise BusinessRuleError(f"Item '{barcode}' has no active loan to renew")

        patron = self._patrons.get_by_card_number(card_number)
        if patron is None or loan.patron_id != patron.id:
            raise BusinessRuleError(f"Loan does not belong to patron with card '{card_number}'")

        loan_period_days, max_renewals = self._get_policy(item)
        if loan.renewal_count >= max_renewals:
            raise BusinessRuleError(
                f"Item '{barcode}' has reached the renewal limit ({max_renewals})"
            )

        loan.due_at = datetime.now(timezone.utc) + timedelta(days=loan_period_days)
        loan.renewal_count += 1
        self._loans.update(loan)
        return loan

    def renew_by_id(self, loan_id: int, patron_id: int | None = None) -> Loan:
        loan = self._loans.get(loan_id)
        if loan is None:
            raise NotFoundError(f"No loan with id={loan_id}")
        if loan.returned_at is not None:
            raise BusinessRuleError(f"Loan {loan_id} has already been returned")
        if patron_id is not None and loan.patron_id != patron_id:
            raise BusinessRuleError("Loan does not belong to this patron")

        item = self._items.get(loan.item_id)
        if item is None:
            raise NotFoundError(f"No item with id={loan.item_id}")

        loan_period_days, max_renewals = self._get_policy(item)
        if loan.renewal_count >= max_renewals:
            raise BusinessRuleError(
                f"Loan {loan_id} has reached the renewal limit ({max_renewals})"
            )

        loan.due_at = datetime.now(timezone.utc) + timedelta(days=loan_period_days)
        loan.renewal_count += 1
        self._loans.update(loan)
        return loan
