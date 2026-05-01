"""Policy edit form submission.

The /ui/policies page renders all loan policies inline, each with a
plain-HTML form. Submitting the form saves changes.

Verifies:
1. The default policy form is visible.
2. Changing loan_period_days and submitting persists the new value.
3. The page re-renders showing the updated value.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

_NEW_LOAN_DAYS = "21"


def test_policy_edit_saves_new_loan_period(librarian_page, e2e_server):
    """Editing loan_period_days on the default policy persists after submit."""
    page = librarian_page
    page.goto(f"{e2e_server}/ui/policies")
    page.wait_for_load_state("networkidle")

    # Find the first loan_period_days input (the seeded default policy)
    loan_days_input = page.locator("input[name=loan_period_days]").first
    assert loan_days_input.is_visible(), "loan_period_days input not found on /ui/policies"

    # Update the value
    loan_days_input.fill(_NEW_LOAN_DAYS)

    # Submit the form (the policy save button; "Save" text distinguishes it from Logout)
    page.locator("button:has-text('Save')").first.click()
    page.wait_for_load_state("networkidle")

    # Should redirect back to the policies page (or stay on it)
    assert "policies" in page.url

    # The updated value should appear in the re-rendered form
    updated_input = page.locator("input[name=loan_period_days]").first
    assert updated_input.input_value() == _NEW_LOAN_DAYS, (
        f"Expected loan_period_days={_NEW_LOAN_DAYS} after save, "
        f"got {updated_input.input_value()!r}"
    )
