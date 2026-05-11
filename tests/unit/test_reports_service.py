"""Unit tests for ReportsService — mock repos, no DB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import csv
import io

from compendium.services.import_export import csv_safe_cell
from compendium.services.reports import ReportsService


def _svc(*, loan_repo=None, item_repo=None, work_repo=None, branch_repo=None):
    return ReportsService(
        loan_repo=loan_repo or MagicMock(),
        item_repo=item_repo or MagicMock(),
        work_repo=work_repo or MagicMock(),
        branch_repo=branch_repo or MagicMock(),
    )


def _fake_work(work_id: int, title: str, media_code: str = "book"):
    return SimpleNamespace(
        id=work_id,
        title=title,
        subtitle=None,
        media_type=SimpleNamespace(code=media_code),
    )


class TestCheckoutsPerMonth:
    def test_fills_empty_months_with_zero(self):
        now = datetime(2026, 4, 15, tzinfo=timezone.utc)
        loans = MagicMock()
        loans.count_checkouts_by_month.return_value = [(2026, 2, 3), (2026, 4, 5)]
        branches = MagicMock()
        branches.get_by_code.return_value = None
        svc = _svc(loan_repo=loans, branch_repo=branches)

        rows = svc.checkouts_per_month(months=4, now=now)

        assert [r.month for r in rows] == ["2026-01", "2026-02", "2026-03", "2026-04"]
        assert [r.count for r in rows] == [0, 3, 0, 5]

    def test_crosses_year_boundary(self):
        now = datetime(2026, 2, 10, tzinfo=timezone.utc)
        loans = MagicMock()
        loans.count_checkouts_by_month.return_value = [(2025, 11, 1), (2026, 2, 4)]
        svc = _svc(loan_repo=loans)

        rows = svc.checkouts_per_month(months=4, now=now)

        assert [r.month for r in rows] == ["2025-11", "2025-12", "2026-01", "2026-02"]
        assert [r.count for r in rows] == [1, 0, 0, 4]

    def test_branch_filter_resolved_to_id(self):
        loans = MagicMock()
        loans.count_checkouts_by_month.return_value = []
        branches = MagicMock()
        branches.get_by_code.return_value = SimpleNamespace(id=7)
        svc = _svc(loan_repo=loans, branch_repo=branches)

        svc.checkouts_per_month(months=1, branch_code="WEST")

        loans.count_checkouts_by_month.assert_called_once()
        kwargs = loans.count_checkouts_by_month.call_args.kwargs
        assert kwargs["branch_id"] == 7


class TestPopularWorks:
    def test_resolves_works_and_preserves_order(self):
        loans = MagicMock()
        loans.popular_works.return_value = [(11, 42), (22, 17)]
        works = MagicMock()
        works.get.side_effect = lambda wid: {
            11: _fake_work(11, "Dune"),
            22: _fake_work(22, "Foundation"),
        }[wid]
        svc = _svc(loan_repo=loans, work_repo=works)

        rows = svc.popular_works(
            since=datetime(2026, 1, 1, tzinfo=timezone.utc), limit=2
        )

        assert [r.title for r in rows] == ["Dune", "Foundation"]
        assert [r.checkout_count for r in rows] == [42, 17]

    def test_skips_missing_works(self):
        loans = MagicMock()
        loans.popular_works.return_value = [(11, 5), (99, 3)]
        works = MagicMock()
        works.get.side_effect = lambda wid: (
            _fake_work(11, "Dune") if wid == 11 else None
        )
        svc = _svc(loan_repo=loans, work_repo=works)

        rows = svc.popular_works(
            since=datetime(2026, 1, 1, tzinfo=timezone.utc), limit=5
        )
        assert [r.work_id for r in rows] == [11]


class TestDormantItems:
    def test_returns_dataclasses_with_branch_code(self):
        items = MagicMock()
        item = SimpleNamespace(id=1, barcode="BC1", branch=SimpleNamespace(code="MAIN"))
        work = _fake_work(1, "Dune")
        items.list_dormant.return_value = [(item, work, None)]
        svc = _svc(item_repo=items)

        rows = svc.dormant_items(
            not_since=datetime(2025, 1, 1, tzinfo=timezone.utc), limit=10
        )

        assert len(rows) == 1
        assert rows[0].barcode == "BC1"
        assert rows[0].branch_code == "MAIN"
        assert rows[0].last_checkout_at is None


class TestCurrentOverdues:
    def test_computes_days_overdue_from_now(self):
        now = datetime(2026, 4, 22, tzinfo=timezone.utc)
        due = now - timedelta(days=5, hours=3)
        loans = MagicMock()
        loans.list_active_overdue_joined.return_value = [
            (
                SimpleNamespace(id=1, due_at=due),
                SimpleNamespace(library_card_number="C1", full_name="Alice"),
                SimpleNamespace(barcode="BC1"),
                _fake_work(1, "Dune"),
            )
        ]
        svc = _svc(loan_repo=loans)

        rows = svc.current_overdues(now=now)

        assert rows[0].days_overdue == 5  # floor of 5 days + 3 hours
        assert rows[0].patron_card == "C1"
        assert rows[0].item_barcode == "BC1"


class TestCsvSafeCell:
    """M3: formula-injection guard for CSV exports."""

    def test_formula_prefix_escaped(self):
        assert csv_safe_cell("=SUM(A1)") == "'=SUM(A1)"

    def test_plus_prefix_escaped(self):
        assert csv_safe_cell("+CMD") == "'+CMD"

    def test_minus_prefix_escaped(self):
        assert csv_safe_cell("-1+1+1") == "'-1+1+1"

    def test_at_prefix_escaped(self):
        assert csv_safe_cell("@SUM") == "'@SUM"

    def test_tab_prefix_escaped(self):
        assert csv_safe_cell("\tcell") == "'\tcell"

    def test_benign_string_unchanged(self):
        assert csv_safe_cell("Normal title") == "Normal title"

    def test_non_string_unchanged(self):
        assert csv_safe_cell(42) == 42
        assert csv_safe_cell(None) is None

    def test_csv_roundtrip_with_formula(self):
        rows = [{"title": "=DANGEROUS", "author": "Bob"}]
        fieldnames = ["title", "author"]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: csv_safe_cell(v) for k, v in row.items()})
        buf.seek(0)
        reader = csv.DictReader(buf)
        result = next(reader)
        assert result["title"].startswith("'"), "Formula should be apostrophe-prefixed"
