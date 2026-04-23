"""Circulation & overdue reports — read-only aggregations over loans/items."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from compendium.repositories.base import (
    BranchRepository,
    ItemRepository,
    LoanRepository,
    WorkRepository,
)


@dataclass
class MonthlyCheckouts:
    month: str  # "YYYY-MM"
    count: int


@dataclass
class PopularWork:
    work_id: int
    title: str
    subtitle: str | None
    media_type_code: str
    checkout_count: int


@dataclass
class DormantItem:
    item_id: int
    barcode: str
    title: str
    media_type_code: str
    branch_code: str
    last_checkout_at: datetime | None


@dataclass
class OverdueLoan:
    loan_id: int
    patron_card: str
    patron_name: str
    item_barcode: str
    title: str
    due_at: datetime
    days_overdue: int


class ReportsService:
    def __init__(
        self,
        *,
        loan_repo: LoanRepository,
        item_repo: ItemRepository,
        work_repo: WorkRepository,
        branch_repo: BranchRepository,
    ) -> None:
        self._loans = loan_repo
        self._items = item_repo
        self._works = work_repo
        self._branches = branch_repo

    def _resolve_branch_id(self, branch_code: str | None) -> int | None:
        if not branch_code:
            return None
        branch = self._branches.get_by_code(branch_code)
        if branch is None:
            return None
        return branch.id

    def checkouts_per_month(
        self,
        *,
        months: int = 12,
        branch_code: str | None = None,
        now: datetime | None = None,
    ) -> list[MonthlyCheckouts]:
        """Monthly checkout counts for the last `months` months (inclusive of current).
        Empty months are included with count=0 so charts draw a continuous series."""
        now = now or datetime.now(tz=timezone.utc)
        # First of the starting month, N months back
        year, month = now.year, now.month
        for _ in range(months - 1):
            month -= 1
            if month == 0:
                month = 12
                year -= 1
        since = datetime(year, month, 1, tzinfo=timezone.utc)
        branch_id = self._resolve_branch_id(branch_code)
        raw = self._loans.count_checkouts_by_month(since=since, branch_id=branch_id)
        by_key = {(y, m): c for y, m, c in raw}
        result: list[MonthlyCheckouts] = []
        y, m = year, month
        for _ in range(months):
            result.append(MonthlyCheckouts(f"{y:04d}-{m:02d}", by_key.get((y, m), 0)))
            m += 1
            if m == 13:
                m = 1
                y += 1
        return result

    def popular_works(
        self,
        *,
        since: datetime,
        until: datetime | None = None,
        limit: int = 20,
        branch_code: str | None = None,
    ) -> list[PopularWork]:
        until = until or datetime.now(tz=timezone.utc)
        branch_id = self._resolve_branch_id(branch_code)
        rows = self._loans.popular_works(
            since=since, until=until, limit=limit, branch_id=branch_id
        )
        out: list[PopularWork] = []
        for work_id, count in rows:
            work = self._works.get(work_id)
            if work is None:
                continue
            out.append(
                PopularWork(
                    work_id=work.id,
                    title=work.title,
                    subtitle=work.subtitle,
                    media_type_code=work.media_type.code,
                    checkout_count=count,
                )
            )
        return out

    def dormant_items(
        self,
        *,
        not_since: datetime,
        limit: int = 100,
        branch_code: str | None = None,
    ) -> list[DormantItem]:
        branch_id = self._resolve_branch_id(branch_code)
        rows = self._items.list_dormant(
            not_since=not_since, limit=limit, branch_id=branch_id
        )
        return [
            DormantItem(
                item_id=item.id,
                barcode=item.barcode,
                title=work.title,
                media_type_code=work.media_type.code,
                branch_code=item.branch.code,
                last_checkout_at=last,
            )
            for item, work, last in rows
        ]

    def current_overdues(
        self,
        *,
        branch_code: str | None = None,
        now: datetime | None = None,
    ) -> list[OverdueLoan]:
        now = now or datetime.now(tz=timezone.utc)
        branch_id = self._resolve_branch_id(branch_code)
        rows = self._loans.list_active_overdue_joined(branch_id=branch_id)
        out: list[OverdueLoan] = []
        for loan, patron, item, work in rows:
            out.append(
                OverdueLoan(
                    loan_id=loan.id,
                    patron_card=patron.library_card_number,
                    patron_name=patron.full_name,
                    item_barcode=item.barcode,
                    title=work.title,
                    due_at=loan.due_at,
                    days_overdue=max(0, (now - loan.due_at).days),
                )
            )
        return out
