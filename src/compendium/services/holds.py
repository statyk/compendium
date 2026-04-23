from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from compendium.domain.enums import HoldStatus, ItemStatus
from compendium.domain.errors import BlockedByFinesError, BusinessRuleError, NotFoundError
from compendium.domain.models import Hold, Item
from compendium.repositories.base import (
    BranchRepository,
    HoldRepository,
    ItemRepository,
    PatronRepository,
    WorkRepository,
)
from compendium.services.fines import CheckoutStatus, FineService

if TYPE_CHECKING:
    from compendium.services.notifications import NotificationService

_TERMINAL = {HoldStatus.FULFILLED.value, HoldStatus.CANCELLED.value, HoldStatus.EXPIRED.value}


class HoldService:
    def __init__(
        self,
        hold_repo: HoldRepository,
        patron_repo: PatronRepository,
        work_repo: WorkRepository,
        branch_repo: BranchRepository,
        item_repo: ItemRepository,
        hold_expiry_days: int = 30,
        hold_pickup_days: int = 3,
        fine_svc: FineService | None = None,
        notification_svc: "NotificationService | None" = None,
    ) -> None:
        self._holds = hold_repo
        self._patrons = patron_repo
        self._works = work_repo
        self._branches = branch_repo
        self._items = item_repo
        self._expiry_days = hold_expiry_days
        self._pickup_days = hold_pickup_days
        self._fines = fine_svc
        self._notifications = notification_svc

    def place(self, work_id: int, card_number: str) -> Hold:
        work = self._works.get(work_id)
        if work is None:
            raise NotFoundError(f"No work with id={work_id}")

        patron = self._patrons.get_by_card_number(card_number)
        if patron is None:
            raise NotFoundError(f"No patron with card number '{card_number}'")
        if not patron.is_active:
            raise BusinessRuleError(f"Patron card '{card_number}' is not active")

        if self._fines is not None:
            status = self._fines.checkout_status(patron)
            if status == CheckoutStatus.BLOCKED:
                raise BlockedByFinesError(
                    patron.library_card_number,
                    self._fines.outstanding_total(patron.id),
                    self._fines._settings.fine_block_threshold_cents or 0,
                )

        if not self._works.has_loanable_item(work_id):
            raise BusinessRuleError("Work has no loanable copies")

        existing = self._holds.get_active_for_patron_work(patron.id, work_id)
        if existing is not None:
            raise BusinessRuleError(f"Patron already has an active hold on work {work_id}")

        branch = self._branches.get_default()
        now = datetime.now(timezone.utc)

        copy = self._works.first_available_loanable_copy(work_id)
        if copy is not None:
            # Immediate promotion: no queue to sit in, the copy is on the shelf.
            hold = Hold(
                work_id=work_id,
                patron_id=patron.id,
                branch_id=branch.id,  # type: ignore[union-attr]
                status=HoldStatus.AVAILABLE.value,
                placed_at=now,
                expires_at=now + timedelta(days=self._pickup_days),
                notified_at=now,
                held_item_id=copy.id,
            )
            copy.status = ItemStatus.ON_HOLD.value
            self._items.update(copy)
            self._holds.add(hold)
            if self._notifications is not None:
                self._notifications.queue_hold_ready(hold)
            return hold

        hold = Hold(
            work_id=work_id,
            patron_id=patron.id,
            branch_id=branch.id,  # type: ignore[union-attr]
            status=HoldStatus.WAITING.value,
            placed_at=now,
            expires_at=now + timedelta(days=self._expiry_days),
        )
        return self._holds.add(hold)

    def cancel(self, hold_id: int, patron_id: int) -> Hold:
        hold = self._holds.get(hold_id)
        if hold is None:
            raise NotFoundError(f"No hold with id={hold_id}")
        if hold.patron_id != patron_id:
            raise BusinessRuleError("Hold does not belong to this patron")
        if hold.status in _TERMINAL:
            raise BusinessRuleError(f"Hold {hold_id} is already {hold.status}")
        self._release_held_item(hold)
        hold.status = HoldStatus.CANCELLED.value
        hold.held_item_id = None
        return self._holds.update(hold)

    def expire_holds(self) -> int:
        holds = self._holds.get_expired_waiting(datetime.now(timezone.utc))
        for hold in holds:
            self._release_held_item(hold)
            hold.status = HoldStatus.EXPIRED.value
            hold.held_item_id = None
            self._holds.update(hold)
        return len(holds)

    def _release_held_item(self, hold: Hold) -> None:
        """If this hold had a copy reserved, either promote the next waiting
        hold onto that copy or free the copy back to AVAILABLE.

        Called during cancel / expire on an AVAILABLE-status hold so we don't
        leave an orphaned ON_HOLD item.
        """
        if hold.held_item_id is None:
            return
        item = self._items.get(hold.held_item_id)
        if item is None:
            return
        next_hold = self._holds.get_oldest_waiting_for_work(item.work_id)
        if next_hold is not None:
            now = datetime.now(timezone.utc)
            next_hold.status = HoldStatus.AVAILABLE.value
            next_hold.held_item_id = item.id
            next_hold.expires_at = now + timedelta(days=self._pickup_days)
            next_hold.notified_at = now
            self._holds.update(next_hold)
            if self._notifications is not None:
                self._notifications.queue_hold_ready(next_hold)
            # item stays ON_HOLD — just for a different patron
        else:
            item.status = ItemStatus.AVAILABLE.value
            self._items.update(item)
