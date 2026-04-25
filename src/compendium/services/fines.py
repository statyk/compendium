"""Fine assessment, payment, and waiver service."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from compendium.config.settings import Settings
from compendium.domain.enums import FineKind, FineStatus
from compendium.domain.errors import NotFoundError, ValidationError
from compendium.domain.models import AppUser, Fine, Item, Loan, Patron
from compendium.repositories.base import (
    FineRepository,
    ItemRepository,
    LoanPolicyRepository,
    LoanRepository,
    PatronRepository,
)
from compendium.services.audit import AuditAction, AuditEntityType, AuditService


class CheckoutStatus(str, Enum):
    OK = "ok"
    BLOCKED = "blocked"
    BLOCKED_AT_PICKUP = "blocked_at_pickup"


class FineService:
    def __init__(
        self,
        *,
        fine_repo: FineRepository,
        patron_repo: PatronRepository,
        loan_repo: LoanRepository,
        item_repo: ItemRepository,
        policy_repo: LoanPolicyRepository,
        settings: Settings,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._fines = fine_repo
        self._patrons = patron_repo
        self._loans = loan_repo
        self._items = item_repo
        self._policies = policy_repo
        self._settings = settings
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list(
        self,
        *,
        patron_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Fine]:
        return self._fines.list(patron_id=patron_id, status=status, limit=limit)

    def outstanding_total(self, patron_id: int) -> int:
        return self._fines.outstanding_total(patron_id)

    def checkout_status(self, patron: Patron) -> CheckoutStatus:
        from compendium.services.site_settings import get_site_setting

        threshold = get_site_setting("fine_block_threshold_cents")
        if threshold is None:
            return CheckoutStatus.OK
        total = self._fines.outstanding_total(patron.id)
        if total <= threshold:
            return CheckoutStatus.OK
        if get_site_setting("fine_block_holds"):
            return CheckoutStatus.BLOCKED
        return CheckoutStatus.BLOCKED_AT_PICKUP

    def projected_overdue_fine(self, loan: Loan) -> int:
        """Compute the current-state overdue fine for an active loan without
        creating a Fine row. Returns 0 if not overdue, no policy, or no rate."""
        if loan.returned_at is not None:
            return 0
        return self._compute_overdue_amount(loan)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def assess_overdue(self, loan: Loan) -> Fine | None:
        """Called at checkin (or on-demand). Creates or updates the single
        outstanding overdue Fine for this loan. Returns the Fine, or None if
        no amount is owed (not overdue, no rate, under grace)."""
        amount = self._compute_overdue_amount(loan)
        existing = self._fines.get_outstanding_overdue_for_loan(loan.id)
        if amount <= 0:
            # Nothing to book. If an existing outstanding Fine is somehow
            # zero or negative, leave it alone — librarian can waive manually.
            return existing
        if existing is not None:
            if existing.amount_cents == amount:
                return existing
            before = existing.amount_cents
            existing.amount_cents = amount
            self._fines.update(existing)
            self._record(
                AuditEntityType.FINE,
                existing.id,
                AuditAction.ASSESS_FINE,
                {
                    "loan_id": loan.id,
                    "kind": FineKind.OVERDUE.value,
                    "amount_cents": amount,
                    "updated_from_cents": before,
                },
            )
            return existing
        fine = Fine(
            patron_id=loan.patron_id,
            loan_id=loan.id,
            item_id=loan.item_id,
            kind=FineKind.OVERDUE.value,
            amount_cents=amount,
            status=FineStatus.OUTSTANDING.value,
        )
        self._fines.add(fine)
        self._record(
            AuditEntityType.FINE,
            fine.id,
            AuditAction.ASSESS_FINE,
            {
                "loan_id": loan.id,
                "kind": FineKind.OVERDUE.value,
                "amount_cents": amount,
            },
        )
        return fine

    def assess_overdue_fines(
        self, patron_id: int | None = None
    ) -> dict[str, int]:
        """Idempotent batch materialization of overdue fines.

        For each currently-overdue active loan (optionally scoped to one patron):
        - if no outstanding overdue Fine exists, create one
        - if one exists with a stale amount, update it
        - paid/waived fines are never touched

        Returns counts: {'created', 'updated', 'unchanged'}.
        """
        created = updated = unchanged = 0
        loans = self._loans.list_active_overdue(patron_id=patron_id)
        for loan in loans:
            existing = self._fines.get_outstanding_overdue_for_loan(loan.id)
            fine = self.assess_overdue(loan)
            if fine is None:
                continue
            if existing is None:
                created += 1
            elif existing.amount_cents != fine.amount_cents or existing is not fine:
                # `assess_overdue` updates in place, so if amounts changed we
                # already recorded an audit entry above.
                updated += 1
            else:
                unchanged += 1
        return {"created": created, "updated": updated, "unchanged": unchanged}

    def assess_lost(
        self,
        item: Item,
        replacement_cost_cents: int | None = None,
        note: str | None = None,
    ) -> list[Fine]:
        """Create a lost-kind Fine for replacement cost (+ optional processing fee).
        ``replacement_cost_cents`` overrides policy default if given. Returns the
        Fines created (primary first, processing second if any)."""
        patron_id = self._last_borrower_patron_id(item)
        if patron_id is None:
            raise ValidationError(
                "Cannot assess lost fee: item has no loan history or current borrower."
            )
        patron = self._patrons.get(patron_id)
        policy = self._resolve_policy_for_item(item, patron)
        cost = replacement_cost_cents
        if cost is None:
            cost = policy.lost_item_default_cents if policy else None
        if cost is None or cost <= 0:
            raise ValidationError(
                "Replacement cost is required (no policy default is configured)."
            )
        fines: list[Fine] = []
        primary = Fine(
            patron_id=patron_id,
            loan_id=self._last_active_or_recent_loan_id(item),
            item_id=item.id,
            kind=FineKind.LOST.value,
            amount_cents=cost,
            status=FineStatus.OUTSTANDING.value,
            note=note,
        )
        self._fines.add(primary)
        fines.append(primary)
        self._record(
            AuditEntityType.FINE,
            primary.id,
            AuditAction.ASSESS_FINE,
            {
                "item_id": item.id,
                "kind": FineKind.LOST.value,
                "amount_cents": cost,
            },
        )
        proc_cents = policy.lost_item_processing_fee_cents if policy else None
        if proc_cents and proc_cents > 0:
            proc = Fine(
                patron_id=patron_id,
                loan_id=primary.loan_id,
                item_id=item.id,
                kind=FineKind.PROCESSING.value,
                amount_cents=proc_cents,
                status=FineStatus.OUTSTANDING.value,
                note="Processing fee for lost item",
            )
            self._fines.add(proc)
            fines.append(proc)
            self._record(
                AuditEntityType.FINE,
                proc.id,
                AuditAction.ASSESS_FINE,
                {
                    "item_id": item.id,
                    "kind": FineKind.PROCESSING.value,
                    "amount_cents": proc_cents,
                },
            )
        return fines

    def assess_damaged(
        self, item: Item, amount_cents: int, note: str
    ) -> Fine:
        if amount_cents <= 0:
            raise ValidationError("amount_cents must be positive")
        if not note or not note.strip():
            raise ValidationError("A note is required when marking damaged.")
        patron_id = self._last_borrower_patron_id(item)
        if patron_id is None:
            raise ValidationError(
                "Cannot assess damaged fee: item has no loan history or current borrower."
            )
        fine = Fine(
            patron_id=patron_id,
            loan_id=self._last_active_or_recent_loan_id(item),
            item_id=item.id,
            kind=FineKind.DAMAGED.value,
            amount_cents=amount_cents,
            status=FineStatus.OUTSTANDING.value,
            note=note.strip(),
        )
        self._fines.add(fine)
        self._record(
            AuditEntityType.FINE,
            fine.id,
            AuditAction.ASSESS_FINE,
            {
                "item_id": item.id,
                "kind": FineKind.DAMAGED.value,
                "amount_cents": amount_cents,
            },
        )
        return fine

    def assess_manual(
        self,
        patron: Patron,
        kind: str,
        amount_cents: int,
        *,
        reason: str | None = None,
        note: str | None = None,
        loan_id: int | None = None,
        item_id: int | None = None,
    ) -> Fine:
        if amount_cents <= 0:
            raise ValidationError("amount_cents must be positive")
        valid = {k.value for k in FineKind}
        if kind not in valid:
            raise ValidationError(
                f"Unknown fine kind '{kind}'. Valid: {sorted(valid)}"
            )
        if kind == FineKind.OTHER.value and not (note and note.strip()):
            raise ValidationError("A note is required when kind is 'other'.")
        fine = Fine(
            patron_id=patron.id,
            loan_id=loan_id,
            item_id=item_id,
            kind=kind,
            amount_cents=amount_cents,
            status=FineStatus.OUTSTANDING.value,
            reason=(reason or None),
            note=(note.strip() if note else None),
        )
        self._fines.add(fine)
        self._record(
            AuditEntityType.FINE,
            fine.id,
            AuditAction.ASSESS_FINE,
            {
                "kind": kind,
                "amount_cents": amount_cents,
                "manual": True,
            },
        )
        return fine

    def pay(self, fine_id: int) -> Fine:
        fine = self._fines.get(fine_id)
        if fine is None:
            raise NotFoundError(f"No fine with id={fine_id}")
        if fine.status != FineStatus.OUTSTANDING.value:
            raise ValidationError(
                f"Fine #{fine.id} is already {fine.status}; cannot pay."
            )
        fine.status = FineStatus.PAID.value
        fine.resolved_at = datetime.now(tz=timezone.utc)
        fine.resolved_by_user_id = self._actor.id if self._actor else None
        self._fines.update(fine)
        self._record(
            AuditEntityType.FINE,
            fine.id,
            AuditAction.PAY_FINE,
            {"amount_cents": fine.amount_cents},
        )
        return fine

    def waive(self, fine_id: int, note: str) -> Fine:
        if not note or not note.strip():
            raise ValidationError("A note is required to waive a fine.")
        fine = self._fines.get(fine_id)
        if fine is None:
            raise NotFoundError(f"No fine with id={fine_id}")
        if fine.status != FineStatus.OUTSTANDING.value:
            raise ValidationError(
                f"Fine #{fine.id} is already {fine.status}; cannot waive."
            )
        fine.status = FineStatus.WAIVED.value
        fine.resolved_at = datetime.now(tz=timezone.utc)
        fine.resolved_by_user_id = self._actor.id if self._actor else None
        existing_note = fine.note or ""
        fine.note = (existing_note + ("\n" if existing_note else "") + f"Waived: {note.strip()}").strip()
        self._fines.update(fine)
        self._record(
            AuditEntityType.FINE,
            fine.id,
            AuditAction.WAIVE_FINE,
            {"amount_cents": fine.amount_cents, "waive_note": note.strip()},
        )
        return fine

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute_overdue_amount(self, loan: Loan) -> int:
        """Calculate overdue fine for a loan given its policy + due_at vs now."""
        policy = (
            self._resolve_policy_for_item(loan.item, loan.patron)
            if loan.item is not None
            else None
        )
        rate = policy.overdue_fine_per_day_cents if policy else None
        if not rate or rate <= 0:
            return 0
        grace = policy.grace_period_days if policy else 0
        # returned_at if set (post-checkin assessment), else now (active overdue)
        reference = loan.returned_at or datetime.now(tz=timezone.utc)
        delta = reference - loan.due_at
        # Whole elapsed days since due_at; undercounts sub-day portions, which is
        # consistent and predictable (a patron returning within 24h of due_at
        # owes 0 days). timedelta.days is floor for positive deltas.
        days_over = max(0, delta.days)
        chargeable_days = max(0, days_over - grace)
        if chargeable_days <= 0:
            return 0
        amount = chargeable_days * rate
        cap = policy.overdue_fine_cap_cents if policy else None
        if cap is not None and cap > 0 and amount > cap:
            amount = cap
        return amount

    def _resolve_policy_for_item(self, item: Item, patron: Patron | None = None):
        category_id = patron.category_id if patron is not None else None
        return self._policies.resolve(item.work.media_type_id, category_id)

    def _last_borrower_patron_id(self, item: Item) -> int | None:
        """Who owes for this item? The patron on the most recent loan
        (active or already returned)."""
        loan = self._loans.get_most_recent_for_item(item.id)
        return loan.patron_id if loan else None

    def _last_active_or_recent_loan_id(self, item: Item) -> int | None:
        loan = self._loans.get_most_recent_for_item(item.id)
        return loan.id if loan else None

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
