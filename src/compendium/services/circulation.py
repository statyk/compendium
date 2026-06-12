from __future__ import annotations

from datetime import datetime, timedelta, timezone

from compendium.domain.enums import HoldStatus, ItemStatus
from compendium.domain.errors import (
    AmbiguousItemError,
    BlockedByFinesError,
    BusinessRuleError,
    HoldQueueBlockError,
    NotFoundError,
    ValidationError,
)
from compendium.domain.models import AppUser, Hold, Item, Loan, Patron, Work
from compendium.repositories.base import (
    BranchRepository,
    HoldRepository,
    ItemNoteRepository,
    ItemRepository,
    LoanPolicyRepository,
    LoanRepository,
    PatronRepository,
    WorkRepository,
)
from compendium.services.audit import AuditAction, AuditEntityType, AuditService
from compendium.services.calendar import CalendarService
from compendium.services.fines import CheckoutStatus, FineService
from compendium.services.notifications import NotificationService
from compendium.services.site_settings import get_site_setting

# max_renewals is still hardcoded — there's no registry descriptor for it
# and no per-deployment knob today. Migrate alongside if ever needed.
_DEFAULT_MAX_RENEWALS = 2


def _upc_variants(upc: str) -> list[str]:
    """Scanned vs stored UPC forms can differ by a leading zero (UPC-A is the
    12-digit subset of EAN-13). Try both."""
    variants = [upc]
    if len(upc) == 13 and upc.startswith("0"):
        variants.append(upc[1:])
    elif len(upc) == 12:
        variants.append("0" + upc)
    return variants


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
        calendar_svc: CalendarService | None = None,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
        item_note_repo: ItemNoteRepository | None = None,
        work_repo: WorkRepository | None = None,
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
        self._calendar = calendar_svc
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source
        self._item_notes = item_note_repo
        self._works = work_repo

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

    def _get_policy(self, item: Item, patron: Patron | None = None) -> tuple[int, int]:
        """Resolve (loan_period_days, max_renewals) for an item ± patron category."""
        category_id = patron.category_id if patron is not None else None
        policy = self._policies.resolve(item.work.media_type_id, category_id)
        if policy is None:
            return get_site_setting("default_loan_period_days"), _DEFAULT_MAX_RENEWALS
        return policy.loan_period_days, policy.max_renewals

    def _fallback_active(self) -> bool:
        return self._works is not None and bool(
            get_site_setting("circulation_scan_isbn_enabled")
        )

    def _code_not_found(self, code: str) -> NotFoundError:
        if self._fallback_active():
            return NotFoundError(f"No item barcode, ISBN, or UPC matching '{code}'")
        return NotFoundError(f"No item with barcode '{code}'")

    def _resolve_work_by_code(self, code: str) -> Work | None:
        """Interpret *code* as an ISBN, then UPC/EAN, and find the matching work.

        Returns None when the fallback is inactive (no work repo wired, or
        the site setting is off) or when nothing matches. A 13-digit code is
        tried as ISBN first — 978/979 Bookland EANs are ISBNs — then as EAN-13.
        """
        if not self._fallback_active():
            return None
        # Local import: keeps `compendium loan ...` CLI startup from paying
        # for the metadata module's HTTP stack when the fallback never fires.
        from compendium.services.metadata import normalize_isbn, normalize_upc

        assert self._works is not None
        try:
            work = self._works.get_by_isbn(normalize_isbn(code))
            if work is not None:
                return work
        except ValidationError:
            pass
        try:
            upc = normalize_upc(code)
        except ValidationError:
            return None
        for candidate in _upc_variants(upc):
            work = self._works.get_by_upc(candidate)
            if work is not None:
                return work
        return None

    def _pick_copy_for_checkout(self, work: Work, patron: Patron, code: str) -> Item:
        """Choose a copy when checkout was requested by ISBN/UPC.

        Preference: the copy already on the pickup shelf for this patron's
        hold, then any loanable AVAILABLE copy (lexicographically first
        accession number — deterministic; holds are work-level so the choice
        is otherwise immaterial)."""
        hold = self._holds.get_available_for_patron_work(patron.id, work.id)
        if hold is not None and hold.held_item_id is not None:
            held = self._items.get(hold.held_item_id)
            if held is not None:
                return held
        candidates = [
            i
            for i in work.items
            if i.is_loanable and i.status == ItemStatus.AVAILABLE
        ]
        if not candidates:
            circulating = [
                i for i in work.items if i.status != ItemStatus.WITHDRAWN
            ]
            raise BusinessRuleError(
                f"No available copy of '{work.title}' for '{code}' "
                f"({len(circulating)} total)"
            )
        return min(candidates, key=lambda i: i.accession_number)

    def _resolve_copy_for_checkin(self, code: str) -> Item:
        """Resolve an ISBN/UPC to the single copy that is currently on loan.

        Never guesses: with several copies out, closing the wrong loan would
        misattribute overdue fines and history, so raise AmbiguousItemError
        and let the desk pick."""
        work = self._resolve_work_by_code(code)
        if work is None:
            raise self._code_not_found(code)
        active = [
            loan
            for i in work.items
            if (loan := self._loans.get_active_for_item(i.id)) is not None
        ]
        if not active:
            raise BusinessRuleError(f"No copies of '{work.title}' are checked out")
        if len(active) > 1:
            raise AmbiguousItemError(code, work.title, active)
        return active[0].item

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
        fallback_work: Work | None = None
        if item is None:
            fallback_work = self._resolve_work_by_code(barcode)
            if fallback_work is None:
                raise self._code_not_found(barcode)

        patron = self._patrons.get_by_card_number(card_number)
        if patron is None:
            raise NotFoundError(f"No patron with card number '{card_number}'")
        if not patron.is_active:
            raise BusinessRuleError(f"Patron card '{card_number}' is not active")
        if patron.expires_at is not None:
            from datetime import date as _date

            if patron.expires_at < _date.today():
                raise BusinessRuleError(
                    f"Patron card '{card_number}' expired on {patron.expires_at.isoformat()}"
                )

        if item is None:
            assert fallback_work is not None
            item = self._pick_copy_for_checkout(fallback_work, patron, code=barcode)

        if self._fines is not None:
            status = self._fines.checkout_status(patron)
            if status != CheckoutStatus.OK:
                from compendium.services.site_settings import get_site_setting

                raise BlockedByFinesError(
                    patron.library_card_number,
                    self._fines.outstanding_total(patron.id),
                    get_site_setting("fine_block_threshold_cents") or 0,
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

        loan_period_days, _ = self._get_policy(item, patron)
        branch = self._branches.get_default()
        now = datetime.now(timezone.utc)
        loan = Loan(
            item_id=item.id,
            patron_id=patron.id,
            branch_id=branch.id,  # type: ignore[union-attr]
            checked_out_at=now,
            due_at=self._compute_due(now, loan_period_days),
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
            item = self._resolve_copy_for_checkin(barcode)

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

        loan_period_days, max_renewals = self._get_policy(item, patron)
        if loan.renewal_count >= max_renewals:
            raise BusinessRuleError(
                f"Item '{barcode}' has reached the renewal limit ({max_renewals})"
            )

        now = datetime.now(timezone.utc)
        loan.due_at = self._compute_due(now, loan_period_days)
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

        # Use the loan's patron for category-aware resolution.
        patron = self._patrons.get(loan.patron_id)
        loan_period_days, max_renewals = self._get_policy(item, patron)
        if loan.renewal_count >= max_renewals:
            raise BusinessRuleError(
                f"Loan {loan_id} has reached the renewal limit ({max_renewals})"
            )

        now = datetime.now(timezone.utc)
        loan.due_at = self._compute_due(now, loan_period_days)
        loan.renewal_count += 1
        self._loans.update(loan)
        return loan

    def _compute_due(self, now_utc: datetime, period_days: int) -> datetime:
        """Compute due_at, rolling forward past closed days when a calendar is configured."""
        if self._calendar is not None:
            return self._calendar.compute_due_at(now_utc, period_days)
        return now_utc + timedelta(days=period_days)

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
        from compendium.services.item_notes import ItemNoteKind, record_system_note

        record_system_note(
            self._item_notes, item.id, ItemNoteKind.STATUS.value, "Item declared lost."
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
        from compendium.services.item_notes import ItemNoteKind, record_system_note

        record_system_note(
            self._item_notes, item.id, ItemNoteKind.STATUS.value, "Item marked as damaged."
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
        from compendium.services.item_notes import ItemNoteKind, record_system_note

        record_system_note(
            self._item_notes, item.id, ItemNoteKind.STATUS.value,
            "Damage cleared; item returned to available.",
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
        from compendium.services.item_notes import ItemNoteKind, record_system_note

        record_system_note(
            self._item_notes, item.id, ItemNoteKind.STATUS.value,
            "Item recovered; status set to available.",
        )
        return item

    # ------------------------------------------------------------------
    # Claims-returned: patron says they returned it, library can't find it
    # ------------------------------------------------------------------

    def claim_returned(self, barcode: str, *, note: str | None = None) -> Item:
        """Mark an actively checked-out item as 'claims-returned'.

        The loan stays open (returned_at unchanged) — overdue fines continue
        to accrue until a librarian resolves the claim. Callable by a patron
        against their own loan (UI enforces ownership) or by a librarian.
        """
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")
        if item.status == ItemStatus.CLAIMS_RETURNED.value:
            raise BusinessRuleError(
                f"Item '{barcode}' is already marked claims-returned."
            )
        if item.status != ItemStatus.CHECKED_OUT.value:
            raise BusinessRuleError(
                f"Item '{barcode}' is not currently checked out "
                f"(current status: {item.status})."
            )
        loan = self._loans.get_active_for_item(item.id)
        if loan is None:
            # Should be unreachable given CHECKED_OUT invariant, but guard
            # defensively — a desynced item.status shouldn't 500.
            raise BusinessRuleError(
                f"Item '{barcode}' has no active loan to claim."
            )

        item.status = ItemStatus.CLAIMS_RETURNED.value
        self._items.update(item)
        self._record(
            AuditEntityType.ITEM,
            item.id,
            AuditAction.CLAIM_RETURNED,
            {
                "barcode": item.barcode,
                "loan_id": loan.id,
                "patron_card": loan.patron.library_card_number if loan.patron else None,
                "note": note,
            },
        )
        from compendium.services.item_notes import ItemNoteKind, record_system_note

        record_system_note(
            self._item_notes, item.id, ItemNoteKind.STATUS.value,
            "Patron claimed item returned.",
        )
        return item

    def verify_returned(self, barcode: str) -> Loan:
        """Librarian resolution: the item was returned after all. Close the
        loan as if normally checked in; audit distinguishes this from a
        routine check-in."""
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")
        if item.status != ItemStatus.CLAIMS_RETURNED.value:
            raise BusinessRuleError(
                f"Item '{barcode}' is not in claims-returned status "
                f"(current: {item.status})."
            )
        loan = self._loans.get_active_for_item(item.id)
        if loan is None:
            raise BusinessRuleError(
                f"Item '{barcode}' has no active loan to resolve."
            )
        loan.returned_at = datetime.now(timezone.utc)
        self._loans.update(loan)
        if self._fines is not None:
            self._fines.assess_overdue(loan)
        self._promote_hold(item)
        self._items.update(item)
        self._record(
            AuditEntityType.ITEM,
            item.id,
            AuditAction.CLAIM_VERIFIED,
            {
                "barcode": item.barcode,
                "loan_id": loan.id,
            },
        )
        from compendium.services.item_notes import ItemNoteKind, record_system_note

        record_system_note(
            self._item_notes, item.id, ItemNoteKind.STATUS.value,
            "Claim verified; item found and returned.",
        )
        return loan

    def write_off_claim(self, barcode: str, *, note: str) -> Loan:
        """Librarian resolution: trust the patron; close the loan without
        declaring lost. Does NOT auto-waive existing fines — librarian handles
        those via the usual waive flow."""
        if not note or not note.strip():
            raise ValidationError("A note is required when writing off a claim.")
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")
        if item.status != ItemStatus.CLAIMS_RETURNED.value:
            raise BusinessRuleError(
                f"Item '{barcode}' is not in claims-returned status "
                f"(current: {item.status})."
            )
        loan = self._loans.get_active_for_item(item.id)
        if loan is None:
            raise BusinessRuleError(
                f"Item '{barcode}' has no active loan to resolve."
            )
        loan.returned_at = datetime.now(timezone.utc)
        self._loans.update(loan)
        # No fine assessment — caller accepts the patron's assertion.
        self._promote_hold(item)
        self._items.update(item)
        self._record(
            AuditEntityType.ITEM,
            item.id,
            AuditAction.CLAIM_WRITE_OFF,
            {
                "barcode": item.barcode,
                "loan_id": loan.id,
                "note": note,
            },
        )
        from compendium.services.item_notes import ItemNoteKind, record_system_note

        record_system_note(
            self._item_notes, item.id, ItemNoteKind.STATUS.value,
            "Patron's claim accepted; item returned to circulation.",
        )
        return loan
