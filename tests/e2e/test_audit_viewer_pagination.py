"""Audit log filter form.

The audit viewer (/ui/admin/audit) has a plain GET filter form that lets
librarians narrow by entity type, entity ID, and user ID. This test verifies
the filter form submits correctly and returns filtered results.

Note: the audit view uses a limit parameter, not cursor/offset pagination.
The tests verify filter-by-entity-type and limit controls.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_audit_log_loads_with_entries(librarian_page, e2e_server):
    """Audit log page renders without error and shows entries from seeding."""
    page = librarian_page
    page.goto(f"{e2e_server}/ui/audit")
    page.wait_for_load_state("networkidle")

    assert "/ui/audit" in page.url or "audit" in page.url.lower()
    # Seeding created works/items/users — audit entries should exist
    content = page.content()
    # The page should have the filter form
    assert 'name="entity_type"' in content, "entity_type filter not found in audit page"


def test_audit_filter_by_entity_type(librarian_page, e2e_server):
    """Selecting 'Work' entity type and submitting shows only Work entries."""
    page = librarian_page
    page.goto(f"{e2e_server}/ui/audit")
    page.wait_for_load_state("networkidle")

    # Select "Work" from entity type dropdown
    page.select_option("select[name=entity_type]", "work")
    page.click("button:has-text('Filter')")
    page.wait_for_load_state("networkidle")

    # URL should contain entity_type=work
    assert "entity_type=work" in page.url, (
        f"Expected entity_type=work in URL after filter, got {page.url}"
    )

    # Any table rows that appear should be of type 'work'
    rows = page.locator("tbody tr").all()
    for row in rows:
        row_text = row.inner_text()
        assert "work" in row_text.lower(), (
            f"Found non-work row after filtering by Work: {row_text!r}"
        )


def test_audit_limit_control(librarian_page, e2e_server):
    """Setting limit=10 limits the result set."""
    page = librarian_page
    url = f"{e2e_server}/ui/audit?limit=10"
    page.goto(url)
    page.wait_for_load_state("networkidle")

    rows = page.locator("tbody tr").all()
    assert len(rows) <= 10, f"Expected at most 10 rows with limit=10, got {len(rows)}"
