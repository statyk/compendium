from __future__ import annotations

from datetime import datetime, timedelta, timezone

from compendium.domain.enums import HoldStatus, ItemStatus
from compendium.domain.errors import (
    BlockedByFinesError,
    BusinessRuleError,
    HoldQueueBlockError,
    NotFoundError,
    ValidationError,
)
from compendium.domain.models import AppUser, Hold, Item, Loan
from compendium.repositories.base import (
    BranchRepository,
    HoldRepository,
    ItemRepository,
    LoanPolicyRepository,
    LoanRepository,
    PatronRepository,
)
from compendium.services.audit import AuditAction, AuditEntityType, AuditService
from compendium.services.fines import CheckoutStatus, FineService
from compendium.services.notifications import NotificationService

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
        fine_svc: FineService | None = None,
        notification_svc: NotificationService | None = None,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._items = item_repo
        self._loans = loan_repo
        self._patrons = patron_repo
        self._branches = branch_repo
        self._holds = hold_repo
        self._policies = policy_repo
        self._pickup_days = hold_pickup_days
        self._fines = fine_svc
        self._notifications = notification_svc
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

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
            hold.held_item_id = item.id
            self._holds.update(hold)
            item.status = ItemStatus.ON_HOLD
            if self._notifications is not None:
                self._notifications.queue_hold_ready(hold)
        else:
            item.status = ItemStatus.AVAILABLE

    def checkout(
        self,
        barcode: str,
        card_number: str,
        *,
        override_holds: bool = False,
    ) -> Loan:
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")

        patron = self._patrons.get_by_card_number(card_number)
        if patron is None:
            raise NotFoundError(f"No patron with card number '{card_number}'")
        if not patron.is_active:
            raise BusinessRuleError(f"Patron card '{card_number}' is not active")

        if self._fines is not None:
            status = self._fines.checkout_status(patron)
            if status != CheckoutStatus.OK:
                raise BlockedByFinesError(
                    patron.library_card_number,
                    self._fines.outstanding_total(patron.id),
                    self._fines._settings.fine_block_threshold_cents or 0,
                )

        if not item.is_loanable:
            raise BusinessRuleError(
                f"Item '{barcode}' is not loanable "
                f"({item.loan_restriction_reason or 'non-circulating'})"
            )

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
        else:
            # AVAILABLE-path: make sure no one's waiting in the hold queue.
            # With immediate-promote in place, reaching this branch with a
            # queued hold implies a race — guard defensively.
            waiting = self._holds.get_oldest_waiting_for_work(item.work_id)
            if waiting is not None and waiting.patron_id != patron.id:
                if not override_holds:
                    raise HoldQueueBlockError(
                        barcode=barcode,
                        waiting_hold_id=waiting.id,
                        waiting_patron_card=waiting.patron.library_card_number,
                    )
                self._record(
                    AuditEntityType.ITEM,
                    item.id,
                    AuditAction.CHECKOUT_OVERRIDE_HOLDS,
                    {
                        "barcode": barcode,
                        "borrower_card": patron.library_card_number,
                        "skipped_hold_id": waiting.id,
                        "skipped_patron_card": waiting.patron.library_card_number,
                    },
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
            fulfilled_hold.held_item_id = None
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

        if self._fines is not None:
            self._fines.assess_overdue(loan)

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

        if self._fines is not None:
            self._fines.assess_overdue(loan)

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

    # ------------------------------------------------------------------
    # Lost / damaged / recovery transitions
    # ------------------------------------------------------------------

    def _close_active_loan(self, item: Item) -> None:
        loan = self._loans.get_active_for_item(item.id)
        if loan is not None:
            loan.returned_at = datetime.now(timezone.utc)
            self._loans.update(loan)

    def _cancel_pending_holds(self, work_id: int) -> list[int]:
        cancelled: list[int] = []
        for hold in self._holds.get_active_for_work(work_id):
            hold.status = HoldStatus.CANCELLED.value
            self._holds.update(hold)
            cancelled.append(hold.id)
        return cancelled

    def declare_lost(
        self,
        barcode: str,
        *,
        replacement_cost_cents: int | None = None,
        note: str | None = None,
    ) -> Item:
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")
        if item.status == ItemStatus.WITHDRAWN.value:
            raise BusinessRuleError(f"Item '{barcode}' is withdrawn; cannot declare lost.")
        if item.status == ItemStatus.LOST.value:
            raise BusinessRuleError(f"Item '{barcode}' is already declared lost.")

        self._close_active_loan(item)
        cancelled = self._cancel_pending_holds(item.work_id)

        fine_ids: list[int] = []
        if self._fines is not None:
            fines = self._fines.assess_lost(
                item, replacement_cost_cents=replacement_cost_cents, note=note
            )
            fine_ids = [f.id for f in fines]

        item.status = ItemStatus.LOST.value
        self._items.update(item)
        self._record(
            AuditEntityType.ITEM,
            item.id,
            AuditAction.DECLARE_LOST,
            {
                "barcode": item.barcode,
                "replacement_cost_cents": replacement_cost_cents,
                "fine_ids": fine_ids,
                "cancelled_hold_ids": cancelled,
                "note": note,
            },
        )
        return item

    def mark_damaged(
        self, barcode: str, *, amount_cents: int, note: str
    ) -> Item:
        if not note or not note.strip():
            raise ValidationError("A note is required when marking damaged.")
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")
        if item.status == ItemStatus.WITHDRAWN.value:
            raise BusinessRuleError(f"Item '{barcode}' is withdrawn; cannot mark damaged.")
        if item.status == ItemStatus.DAMAGED.value:
            raise BusinessRuleError(f"Item '{barcode}' is already marked damaged.")

        self._close_active_loan(item)
        cancelled = self._cancel_pending_holds(item.work_id)

        fine_id: int | None = None
        if self._fines is not None:
            fine = self._fines.assess_damaged(item, amount_cents=amount_cents, note=note)
            fine_id = fine.id

        item.status = ItemStatus.DAMAGED.value
        self._items.update(item)
        self._record(
            AuditEntityType.ITEM,
            item.id,
            AuditAction.MARK_DAMAGED,
            {
                "barcode": item.barcode,
                "amount_cents": amount_cents,
                "fine_id": fine_id,
                "cancelled_hold_ids": cancelled,
                "note": note,
            },
        )
        return item

    def clear_damage(self, barcode: str) -> Item:
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")
        if item.status != ItemStatus.DAMAGED.value:
            raise BusinessRuleError(
                f"Item '{barcode}' is not in damaged status (current: {item.status})."
            )
        item.status = ItemStatus.AVAILABLE.value
        self._items.update(item)
        self._record(
            AuditEntityType.ITEM,
            item.id,
            AuditAction.CLEAR_DAMAGE,
            {"barcode": item.barcode},
        )
        return item

    def clear_lost(self, barcode: str) -> Item:
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")
        if item.status != ItemStatus.LOST.value:
            raise BusinessRuleError(
                f"Item '{barcode}' is not in lost status (current: {item.status})."
            )
        item.status = ItemStatus.AVAILABLE.value
        self._items.update(item)
        self._record(
            AuditEntityType.ITEM,
            item.id,
            AuditAction.CLEAR_LOST,
            {"barcode": item.barcode},
        )
        return item
