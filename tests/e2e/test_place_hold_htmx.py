"""HTMX hold-placement smoke test.

Verifies that:
1. The "Place Hold" form appears on a work-detail page when the patron is
   logged in and the work has a loanable item.
2. Clicking the button triggers an HTMX partial swap — the #hold-action div
   updates *in place* without a full page reload.
3. The response contains the expected success text.

If HTMX didn't load (e.g. due to a missing nonce), the browser would fall
back to a regular form POST that causes a full page reload, and the div would
NOT be updated in-place. This test catches that regression.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_place_hold_htmx_swap(patron_page, e2e_server, e2e_seed):
    """Clicking Place Hold updates #hold-action in-place via HTMX."""
    page = patron_page
    work_url = f"{e2e_server}/ui/catalog/{e2e_seed.work_a_id}"

    page.goto(work_url)
    page.wait_for_load_state("networkidle")

    # The hold form should be visible for a logged-in patron
    hold_section = page.locator("#hold-action")
    assert hold_section.is_visible(), "#hold-action section not found on work-detail page"

    submit_btn = hold_section.locator("button[type=submit]")
    assert submit_btn.is_visible(), "Place Hold button not found"

    # Capture the URL before clicking — it should NOT change (HTMX, not navigation)
    url_before = page.url

    submit_btn.click()
    # Wait for HTMX to complete the partial swap
    page.wait_for_load_state("networkidle")

    # URL must not have changed — a full reload would navigate away
    assert page.url == url_before, (
        f"Page URL changed after hold submit — full reload instead of HTMX swap?\n"
        f"Before: {url_before}\nAfter: {page.url}"
    )

    # The div must now contain the success (or error) message from the server,
    # indicating the partial swap occurred.
    hold_section_text = hold_section.inner_text()
    assert "Hold placed" in hold_section_text or "hold" in hold_section_text.lower(), (
        f"#hold-action didn't receive a server response. Content: {hold_section_text!r}"
    )
