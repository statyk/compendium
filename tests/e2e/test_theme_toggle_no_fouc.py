"""Theme toggle — no flash of unstyled content (FOUC).

The pre-paint inline script in base.html reads localStorage on first render
and applies `data-theme` to `<html>` before the browser draws the page.

Verifies:
1. When localStorage is pre-set to 'dark', the data-theme attribute is 'dark'
   immediately after page load (no FOUC — the pre-paint script ran correctly).
2. Switching via the theme dropdown updates data-theme and localStorage.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_dark_theme_applied_immediately_from_localstorage(page, e2e_server):
    """Pre-paint script must set data-theme=dark before first paint."""
    # Set the preference before any page load so the pre-paint script sees it
    page.add_init_script("localStorage.setItem('compendium_theme', 'dark')")

    page.goto(f"{e2e_server}/ui/login")
    page.wait_for_load_state("domcontentloaded")

    theme = page.evaluate("document.documentElement.getAttribute('data-theme')")
    assert theme == "dark", (
        f"Expected data-theme='dark' immediately after load (pre-paint script), got {theme!r}"
    )


def test_theme_switch_to_light_updates_dom_and_storage(page, e2e_server, e2e_seed):
    """Switching from dark to light via the theme menu updates data-theme + localStorage."""
    page.add_init_script("localStorage.setItem('compendium_theme', 'dark')")

    page.goto(f"{e2e_server}/ui/login")
    page.fill("input[name=username]", e2e_seed.librarian_username)
    page.fill("input[name=password]", e2e_seed.librarian_password)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

    # Open the theme dropdown (<details id="theme-menu">)
    page.click("#theme-menu > summary")
    # Click the "Light" option
    page.click("[data-set-theme='light']")

    theme = page.evaluate("document.documentElement.getAttribute('data-theme')")
    stored = page.evaluate("localStorage.getItem('compendium_theme')")

    assert theme == "light", f"data-theme should be 'light' after clicking Light, got {theme!r}"
    assert stored == "light", f"localStorage['compendium_theme'] should be 'light', got {stored!r}"
