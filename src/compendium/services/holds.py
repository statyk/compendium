from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from compendium.domain.enums import HoldStatus, ItemStatus
from compendium.domain.errors import (
    BlockedByFinesError,
    BusinessRuleError,
    NotFoundError,
    ValidationError,
)
from compendium.domain.models import AppUser, Hold, Item
from compendium.repositories.base import (
    BranchRepository,
    HoldRepository,
    ItemRepository,
    PatronRepository,
    WorkRepository,
)
from compendium.services.audit import AuditAction, AuditEntityType, AuditService
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
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
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

    # ------------------------------------------------------------------
    # Suspend / resume: patron parks a WAITING hold (vacation etc.)
    # ------------------------------------------------------------------

    def suspend(
        self,
        hold_id: int,
        *,
        until: date,
        patron_id: int | None = None,
        reason: str | None = None,
    ) -> Hold:
        """Suspend a WAITING hold until the given date. The queue will skip
        this hold when promoting; auto-resumes on/after ``until``.

        If ``patron_id`` is given, enforce that the hold belongs to that
        patron (used by /me self-service routes)."""
        hold = self._holds.get(hold_id)
        if hold is None:
            raise NotFoundError(f"No hold with id={hold_id}")
        if patron_id is not None and hold.patron_id != patron_id:
            raise BusinessRuleError("Hold does not belong to this patron")
        if hold.status != HoldStatus.WAITING.value:
            raise BusinessRuleError(
                f"Only waiting holds can be suspended (current status: {hold.status})."
            )
        if until <= date.today():
            raise ValidationError("Suspension end date must be in the future.")
        reason_clean = (reason or "").strip() or None
        if reason_clean and len(reason_clean) > 256:
            raise ValidationError("Reason must be 256 characters or fewer.")
        hold.suspended_until = until
        hold.suspended_reason = reason_clean
        updated = self._holds.update(hold)
        self._record(
            AuditEntityType.PATRON,
            hold.patron_id,
            AuditAction.HOLD_SUSPEND,
            {
                "hold_id": hold.id,
                "work_id": hold.work_id,
                "suspended_until": until.isoformat(),
                "reason": reason_clean,
            },
        )
        return updated

    def resume(self, hold_id: int, *, patron_id: int | None = None) -> Hold:
        """Clear the suspension on a hold. If a loanable copy is now
        available, immediately promote it (same path as place() does for
        first-time holds); otherwise it re-enters the queue at its original
        placed_at position."""
        hold = self._holds.get(hold_id)
        if hold is None:
            raise NotFoundError(f"No hold with id={hold_id}")
        if patron_id is not None and hold.patron_id != patron_id:
            raise BusinessRuleError("Hold does not belong to this patron")
        if hold.suspended_until is None:
            raise BusinessRuleError(f"Hold {hold_id} is not suspended.")
        if hold.status != HoldStatus.WAITING.value:
            raise BusinessRuleError(
                f"Cannot resume a hold in status {hold.status}."
            )
        prior_until = hold.suspended_until.isoformat()
        hold.suspended_until = None
        hold.suspended_reason = None
        # If a copy is available, promote in place.
        copy = self._works.first_available_loanable_copy(hold.work_id)
        if copy is not None:
            now = datetime.now(timezone.utc)
            hold.status = HoldStatus.AVAILABLE.value
            hold.held_item_id = copy.id
            hold.expires_at = now + timedelta(days=self._pickup_days)
            hold.notified_at = now
            copy.status = ItemStatus.ON_HOLD.value
            self._items.update(copy)
            if self._notifications is not None:
                self._notifications.queue_hold_ready(hold)
        updated = self._holds.update(hold)
        self._record(
            AuditEntityType.PATRON,
            hold.patron_id,
            AuditAction.HOLD_RESUME,
            {
                "hold_id": hold.id,
                "work_id": hold.work_id,
                "resumed_from": prior_until,
                "promoted": hold.status == HoldStatus.AVAILABLE.value,
            },
        )
        return updated

    def resume_expired_suspends(
        self, *, today: date | None = None, dry_run: bool = False
    ) -> list[Hold]:
        """Auto-resume holds whose suspended_until <= today. Returns the list
        of holds that match (whether or not dry_run). On non-dry-run, each
        matching hold is resumed (and auto-promoted if a copy is available).
        Emits one HOLD_RESUME audit entry per hold."""
        cutoff = today or date.today()
        matches = self._holds.list_suspended_expiring_on_or_before(cutoff)
        if dry_run:
            return matches
        resumed: list[Hold] = []
        for hold in matches:
            # resume() would re-audit; we want a single "auto" marker. Do the
            # resume inline here so the audit detail can tag `reason=auto`.
            prior_until = hold.suspended_until.isoformat() if hold.suspended_until else None
            hold.suspended_until = None
            hold.suspended_reason = None
            copy = self._works.first_available_loanable_copy(hold.work_id)
            if copy is not None:
                now = datetime.now(timezone.utc)
                hold.status = HoldStatus.AVAILABLE.value
                hold.held_item_id = copy.id
                hold.expires_at = now + timedelta(days=self._pickup_days)
                hold.notified_at = now
                copy.status = ItemStatus.ON_HOLD.value
                self._items.update(copy)
                if self._notifications is not None:
                    self._notifications.queue_hold_ready(hold)
            self._holds.update(hold)
            self._record(
                AuditEntityType.PATRON,
                hold.patron_id,
                AuditAction.HOLD_RESUME,
                {
                    "hold_id": hold.id,
                    "work_id": hold.work_id,
                    "resumed_from": prior_until,
                    "promoted": hold.status == HoldStatus.AVAILABLE.value,
                    "reason": "auto",
                },
            )
            resumed.append(hold)
        return resumed

    # ------------------------------------------------------------------
    # Librarian list views
    # ------------------------------------------------------------------

    def list_active(
        self,
        *,
        status: str | None = None,
        branch_id: int | None = None,
        work_id: int | None = None,
        patron_id: int | None = None,
        query: str | None = None,
        older_than_days: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Hold]:
        return self._holds.list_active(
            status=status,
            branch_id=branch_id,
            work_id=work_id,
            patron_id=patron_id,
            query=query,
            older_than_days=older_than_days,
            limit=limit,
            offset=offset,
        )

    def count_active(
        self,
        *,
        status: str | None = None,
        branch_id: int | None = None,
        work_id: int | None = None,
        patron_id: int | None = None,
        query: str | None = None,
        older_than_days: int | None = None,
    ) -> int:
        return self._holds.count_active(
            status=status,
            branch_id=branch_id,
            work_id=work_id,
            patron_id=patron_id,
            query=query,
            older_than_days=older_than_days,
        )

    def queue_for_work(self, work_id: int) -> list[Hold]:
        return self._holds.queue_for_work(work_id)

    def queue_position(self, hold_id: int) -> int | None:
        return self._holds.queue_position(hold_id)

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
