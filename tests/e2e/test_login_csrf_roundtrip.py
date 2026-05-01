"""Login + CSRF cookie round-trip.

Verifies:
- Login page renders a csrf_token hidden input.
- Valid credentials redirect to /ui/catalog.
- The auth cookie is set HttpOnly.
- Authenticated pages are accessible after login.
- Logout clears the auth cookie.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_login_form_has_csrf_input(page, e2e_server):
    page.goto(f"{e2e_server}/ui/login")
    page.wait_for_load_state("domcontentloaded")
    # CSRF token is a hidden input in the login form
    csrf = page.query_selector("input[name=csrf_token]")
    assert csrf is not None, "Login form missing csrf_token hidden input"
    assert csrf.get_attribute("value"), "csrf_token value is empty"


def test_login_redirects_to_catalog(page, e2e_server, e2e_seed):
    page.goto(f"{e2e_server}/ui/login")
    page.fill("input[name=username]", e2e_seed.librarian_username)
    page.fill("input[name=password]", e2e_seed.librarian_password)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    assert "/ui/catalog" in page.url, f"Expected redirect to /ui/catalog, got {page.url}"


def test_auth_cookie_is_httponly(page, e2e_server, e2e_seed):
    page.goto(f"{e2e_server}/ui/login")
    page.fill("input[name=username]", e2e_seed.librarian_username)
    page.fill("input[name=password]", e2e_seed.librarian_password)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

    cookies = page.context.cookies()
    auth_cookie = next((c for c in cookies if c["name"] == "compendium_auth"), None)
    assert auth_cookie is not None, "compendium_auth cookie not set after login"
    assert auth_cookie["httpOnly"], "compendium_auth cookie must be HttpOnly"


def test_me_loans_accessible_after_login(page, e2e_server, e2e_seed):
    page.goto(f"{e2e_server}/ui/login")
    page.fill("input[name=username]", e2e_seed.patron_username)
    page.fill("input[name=password]", e2e_seed.patron_password)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

    page.goto(f"{e2e_server}/ui/me/loans")
    page.wait_for_load_state("networkidle")
    assert "/ui/me/loans" in page.url, f"Expected /ui/me/loans, got {page.url}"


def test_logout_clears_auth_cookie(page, e2e_server, e2e_seed):
    # Log in first
    page.goto(f"{e2e_server}/ui/login")
    page.fill("input[name=username]", e2e_seed.librarian_username)
    page.fill("input[name=password]", e2e_seed.librarian_password)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

    # Submit the logout form (it lives inside a <details> dropdown; submit directly)
    page.evaluate("document.querySelector(\"form[action='/ui/logout']\").submit()")
    page.wait_for_load_state("networkidle")

    cookies = page.context.cookies()
    auth_cookie = next((c for c in cookies if c["name"] == "compendium_auth"), None)
    assert auth_cookie is None or not auth_cookie.get("value"), (
        "compendium_auth cookie should be cleared after logout"
    )
