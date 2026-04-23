"""Integration tests for ReportsService against SQLite (with real repos)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from compendium.domain.enums import ItemStatus
from compendium.domain.models import Item, Loan, MediaType, Patron, Work
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.reports import ReportsService


_counter = {"n": 0}


def _next_id() -> int:
    _counter["n"] += 1
    return _counter["n"]


def _add_work_and_item(session, *, title):
    n = _next_id()
    book_type = session.query(MediaType).filter_by(code="book").one()
    work = Work(title=title, media_type_id=book_type.id)
    session.add(work)
    session.flush()
    branch = SqlBranchRepository(session).get_default()
    item = Item(
        work_id=work.id,
        branch_id=branch.id,
        barcode=f"BC{n:06d}",
        accession_number=f"ACC{n:06d}",
    )
    session.add(item)
    session.flush()
    return work, item


def _patron(session, card):
    p = Patron(library_card_number=card, full_name="Alice")
    session.add(p)
    session.flush()
    return p


def _loan(session, *, item, patron, checked_out_at, due_at, returned_at=None):
    branch = SqlBranchRepository(session).get_default()
    loan = Loan(
        item_id=item.id,
        patron_id=patron.id,
        branch_id=branch.id,
        checked_out_at=checked_out_at,
        due_at=due_at,
        returned_at=returned_at,
    )
    session.add(loan)
    session.flush()
    return loan


def _svc(session) -> ReportsService:
    return ReportsService(
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
    )


class TestCheckoutsPerMonth:
    def test_counts_and_fills_gaps(self, session):
        _, item = _add_work_and_item(session, title="A")
        patron = _patron(session, "C1")
        now = datetime(2026, 4, 15, tzinfo=timezone.utc)

        _loan(
            session,
            item=item,
            patron=patron,
            checked_out_at=datetime(2026, 2, 5, tzinfo=timezone.utc),
            due_at=datetime(2026, 2, 19, tzinfo=timezone.utc),
            returned_at=datetime(2026, 2, 10, tzinfo=timezone.utc),
        )
        _loan(
            session,
            item=item,
            patron=patron,
            checked_out_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            due_at=datetime(2026, 4, 15, tzinfo=timezone.utc),
        )

        rows = _svc(session).checkouts_per_month(months=4, now=now)
        by_month = {r.month: r.count for r in rows}
        assert by_month == {"2026-01": 0, "2026-02": 1, "2026-03": 0, "2026-04": 1}


class TestPopularWorks:
    def test_orders_by_checkout_count_desc(self, session):
        _, item_a = _add_work_and_item(session, title="Dune")
        _, item_b = _add_work_and_item(session, title="Foundation")
        patron = _patron(session, "C2")

        # Dune: 3 loans. Foundation: 1.
        for _ in range(3):
            _loan(
                session,
                item=item_a,
                patron=patron,
                checked_out_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
                due_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
                returned_at=datetime(2026, 3, 10, tzinfo=timezone.utc),
            )
        _loan(
            session,
            item=item_b,
            patron=patron,
            checked_out_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            due_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
        )

        rows = _svc(session).popular_works(
            since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 1, tzinfo=timezone.utc),
            limit=10,
        )
        assert [r.title for r in rows] == ["Dune", "Foundation"]
        assert rows[0].checkout_count == 3
        assert rows[1].checkout_count == 1

    def test_respects_date_window(self, session):
        _, item = _add_work_and_item(session, title="A")
        patron = _patron(session, "C3")
        # Out of window
        _loan(
            session,
            item=item,
            patron=patron,
            checked_out_at=datetime(2025, 12, 1, tzinfo=timezone.utc),
            due_at=datetime(2025, 12, 15, tzinfo=timezone.utc),
            returned_at=datetime(2025, 12, 10, tzinfo=timezone.utc),
        )
        rows = _svc(session).popular_works(
            since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            until=datetime(2026, 5, 1, tzinfo=timezone.utc),
            limit=10,
        )
        assert rows == []


class TestDormantItems:
    def test_never_loaned_items_appear(self, session):
        _, item = _add_work_and_item(session, title="A")

        rows = _svc(session).dormant_items(
            not_since=datetime(2025, 1, 1, tzinfo=timezone.utc),
            limit=10,
        )
        barcodes = [r.barcode for r in rows]
        assert item.barcode in barcodes
        # Never-loaned → None
        (row,) = [r for r in rows if r.barcode == item.barcode]
        assert row.last_checkout_at is None

    def test_recently_loaned_item_excluded(self, session):
        _, item = _add_work_and_item(session, title="A")
        patron = _patron(session, "C4")
        _loan(
            session,
            item=item,
            patron=patron,
            checked_out_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            due_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
            returned_at=datetime(2026, 3, 10, tzinfo=timezone.utc),
        )

        rows = _svc(session).dormant_items(
            not_since=datetime(2026, 1, 1, tzinfo=timezone.utc),
            limit=50,
        )
        barcodes = [r.barcode for r in rows]
        assert item.barcode not in barcodes

    def test_withdrawn_items_excluded(self, session):
        _, item = _add_work_and_item(session, title="A")
        item.status = ItemStatus.WITHDRAWN.value
        session.flush()

        rows = _svc(session).dormant_items(
            not_since=datetime(2025, 1, 1, tzinfo=timezone.utc),
            limit=50,
        )
        assert all(r.barcode != item.barcode for r in rows)

    def test_nulls_first_ordering(self, session):
        # Item A: never loaned. Item B: loaned once, long ago.
        _, item_a = _add_work_and_item(session, title="A")
        _, item_b = _add_work_and_item(session, title="B")
        patron = _patron(session, "C5")
        _loan(
            session,
            item=item_b,
            patron=patron,
            checked_out_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            due_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
            returned_at=datetime(2024, 1, 10, tzinfo=timezone.utc),
        )
        rows = _svc(session).dormant_items(
            not_since=datetime(2025, 1, 1, tzinfo=timezone.utc),
            limit=50,
        )
        # Find item_a and item_b in the result
        idx_a = next(i for i, r in enumerate(rows) if r.barcode == item_a.barcode)
        idx_b = next(i for i, r in enumerate(rows) if r.barcode == item_b.barcode)
        assert idx_a < idx_b  # never-loaned sorts before oldest-loan


class TestCurrentOverdues:
    def test_lists_only_overdue_active_loans(self, session):
        _, item1 = _add_work_and_item(session, title="A")
        _, item2 = _add_work_and_item(session, title="B")
        _, item3 = _add_work_and_item(session, title="C")
        patron = _patron(session, "C6")
        now = datetime.now(tz=timezone.utc)

        # Overdue active
        overdue_loan = _loan(
            session,
            item=item1,
            patron=patron,
            checked_out_at=now - timedelta(days=20),
            due_at=now - timedelta(days=5),
        )
        # Active but not overdue
        _loan(
            session,
            item=item2,
            patron=patron,
            checked_out_at=now - timedelta(days=2),
            due_at=now + timedelta(days=12),
        )
        # Overdue but returned — excluded
        _loan(
            session,
            item=item3,
            patron=patron,
            checked_out_at=now - timedelta(days=30),
            due_at=now - timedelta(days=15),
            returned_at=now - timedelta(days=10),
        )

        rows = _svc(session).current_overdues(now=now)
        assert len(rows) == 1
        assert rows[0].loan_id == overdue_loan.id
        assert rows[0].days_overdue == 5
        assert rows[0].title == "A"
