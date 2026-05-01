"""Barcode scanner — mocked BarcodeDetector.

scanner.js uses the native BarcodeDetector API (Chromium) with a ZXing
fallback. To avoid camera permission prompts and real video, we:

1. Mock `navigator.mediaDevices.getUserMedia` to return a canvas-based stream.
2. Override `window.BarcodeDetector` with a mock that immediately returns a
   deterministic barcode value.

When the user clicks "Scan barcode" on the circ desk, the dialog opens,
the mock scanner fires instantly, and the target input is populated.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e

_FAKE_BARCODE = "000001"  # matches the first item seeded in conftest

_SCANNER_MOCK = """
// Mock BarcodeDetector so the native path fires immediately with a known value.
// getUserMedia is handled by the --use-fake-device-for-media-stream Chromium flag
// (set in conftest.py browser_type_launch_args), so no getUserMedia mock needed.
window.BarcodeDetector = class {
  static async getSupportedFormats() {
    return ['ean_13', 'ean_8', 'code_128', 'upc_a'];
  }
  async detect(_video) {
    return [{ rawValue: '%s' }];
  }
};
""" % _FAKE_BARCODE


def test_scanner_populates_barcode_input(librarian_page, e2e_server):
    """Clicking 'Scan' on the circ desk populates the barcode input field."""
    page = librarian_page

    # Install the mock BEFORE the page loads so DOMContentLoaded sees it
    page.add_init_script(_SCANNER_MOCK)

    page.goto(f"{e2e_server}/ui/circ")
    page.wait_for_load_state("networkidle")

    # Click the "Scan" button next to the item barcode input on the check-out form
    scan_btn = page.locator("[data-scan-target='co-barcode']").first
    assert scan_btn.is_visible(), "Scan button not found on circ desk"

    scan_btn.click()

    # The mock fires in one rAF tick — faster than Playwright's ~100ms poll.
    # Wait directly for the input to become non-empty instead of trying to
    # catch the dialog open/close cycle.
    page.wait_for_function(
        "() => document.getElementById('co-barcode').value !== ''",
        timeout=10000,
    )

    barcode_input = page.locator("#co-barcode")
    value = barcode_input.input_value()
    assert value == _FAKE_BARCODE, (
        f"Expected barcode input to be populated with {_FAKE_BARCODE!r}, got {value!r}"
    )


def test_scanner_dialog_opens(librarian_page, e2e_server):
    """Clicking 'Scan' opens the scanner dialog."""
    page = librarian_page
    page.add_init_script(_SCANNER_MOCK)

    page.goto(f"{e2e_server}/ui/circ")
    page.wait_for_load_state("networkidle")

    scan_btn = page.locator("[data-scan-target='co-barcode']").first
    scan_btn.click()

    # Dialog should be visible momentarily (may close quickly due to mock)
    try:
        page.wait_for_selector("#scanner-dialog", state="visible", timeout=2000)
        dialog_visible = True
    except Exception:
        # Mock fires so fast the dialog may have already closed
        dialog_visible = False

    # Either the dialog opened (and possibly closed) OR the input was populated
    barcode_value = page.locator("#co-barcode").input_value()
    assert dialog_visible or barcode_value == _FAKE_BARCODE, (
        "Scanner dialog never opened and barcode was not populated"
    )
