"""CSP console-error watcher — the keystone E2E test.

Each test navigates to a page, waits for it to settle, and asserts that no
CSP violations (or other JS errors) appeared in the browser console.

This is the test that would have caught the 2026-04-30 bug where
`'strict-dynamic'` silently blocked HTMX because external `<script src>`
tags were missing nonces.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

# Pages reachable anonymously (guest search enabled by default)
_ANON_PAGES = [
    "/ui/login",
    "/ui/catalog",
]

# Pages that require librarian auth (Administrator role — no patron record required)
_AUTH_PAGES = [
    "/ui/catalog",  # also test as librarian
    "/ui/circ",
    "/ui/items/new",
    "/ui/admin/holds",
    "/ui/admin/loans",
    "/ui/admin/fines",
    "/ui/audit",
    "/ui/admin/settings/general",
    "/ui/admin/system/security",
    "/ui/reports/checkouts",
    "/ui/reports/popular",
    "/ui/policies",
]

# Pages that require a patron record (only the patron_user has one)
_PATRON_PAGES = [
    "/ui/me/loans",
    "/ui/me/holds",
]


@pytest.mark.parametrize("path", _ANON_PAGES)
def test_csp_anonymous_pages(page, e2e_server, path):
    """Anonymous page loads must not produce console errors."""
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(f"{e2e_server}{path}")
    page.wait_for_load_state("networkidle")

    assert not errors, (
        f"Console errors on anonymous {path}:\n" + "\n".join(errors)
    )


@pytest.mark.parametrize("path", _AUTH_PAGES)
def test_csp_librarian_pages(librarian_page, e2e_server, path):
    """Authenticated page loads must not produce console errors."""
    errors: list[str] = []
    librarian_page.on(
        "console", lambda msg: errors.append(msg.text) if msg.type == "error" else None
    )
    librarian_page.on("pageerror", lambda exc: errors.append(str(exc)))

    librarian_page.goto(f"{e2e_server}{path}")
    librarian_page.wait_for_load_state("networkidle")

    assert not errors, (
        f"Console errors on librarian {path}:\n" + "\n".join(errors)
    )


@pytest.mark.parametrize("path", _PATRON_PAGES)
def test_csp_patron_pages(patron_page, e2e_server, path):
    """Patron self-service pages must not produce console errors."""
    errors: list[str] = []
    patron_page.on(
        "console", lambda msg: errors.append(msg.text) if msg.type == "error" else None
    )
    patron_page.on("pageerror", lambda exc: errors.append(str(exc)))

    patron_page.goto(f"{e2e_server}{path}")
    patron_page.wait_for_load_state("networkidle")

    assert not errors, (
        f"Console errors on patron {path}:\n" + "\n".join(errors)
    )
