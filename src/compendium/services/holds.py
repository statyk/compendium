from __future__ import annotations

from datetime import datetime, timedelta, timezone

from compendium.domain.enums import HoldStatus
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import Hold
from compendium.repositories.base import (
    BranchRepository,
    HoldRepository,
    PatronRepository,
    WorkRepository,
)

_TERMINAL = {HoldStatus.FULFILLED.value, HoldStatus.CANCELLED.value, HoldStatus.EXPIRED.value}


class HoldService:
    def __init__(
        self,
        hold_repo: HoldRepository,
        patron_repo: PatronRepository,
        work_repo: WorkRepository,
        branch_repo: BranchRepository,
        hold_expiry_days: int = 30,
    ) -> None:
        self._holds = hold_repo
        self._patrons = patron_repo
        self._works = work_repo
        self._branches = branch_repo
        self._expiry_days = hold_expiry_days

    def place(self, work_id: int, card_number: str) -> Hold:
        work = self._works.get(work_id)
        if work is None:
            raise NotFoundError(f"No work with id={work_id}")

        patron = self._patrons.get_by_card_number(card_number)
        if patron is None:
            raise NotFoundError(f"No patron with card number '{card_number}'")
        if not patron.is_active:
            raise BusinessRuleError(f"Patron card '{card_number}' is not active")

        if not self._works.has_loanable_item(work_id):
            raise BusinessRuleError("Work has no loanable copies")

        existing = self._holds.get_active_for_patron_work(patron.id, work_id)
        if existing is not None:
            raise BusinessRuleError(f"Patron already has an active hold on work {work_id}")

        branch = self._branches.get_default()
        now = datetime.now(timezone.utc)
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
        hold.status = HoldStatus.CANCELLED.value
        return self._holds.update(hold)

    def expire_holds(self) -> int:
        holds = self._holds.get_expired_waiting(datetime.now(timezone.utc))
        for hold in holds:
            hold.status = HoldStatus.EXPIRED.value
            self._holds.update(hold)
        return len(holds)
