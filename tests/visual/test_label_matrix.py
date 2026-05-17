"""
Visual label matrix test.

Runs the render_label_matrix.py script (which boots a temp server and uses
Playwright to screenshot every kind × template × field-set combination) and
then reads each PNG so Claude can verify layout sanity visually.

Run with:
    uv run pytest -m visual -v

The test itself only asserts structural correctness (all expected files exist,
all are non-empty).  The visual inspection — "does the text actually fit and
look right?" — is done by reading the PNGs in the test output and reviewing
them in the same Claude session that made the change.

Prerequisites:
    uv sync --extra e2e
    uv run playwright install chromium
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
OUT_DIR = PROJECT_ROOT / "out" / "labels"
SCRIPT = PROJECT_ROOT / "scripts" / "render_label_matrix.py"

# Derived from FIELD_SETS in the script (3 field-set labels × all templates).
# Listed here so the test fails loudly if the script stops producing output
# for any of the expected combinations.
EXPECTED_COMBINATIONS = [
    # spine
    ("spine", "avery-5160",       "none"),
    ("spine", "avery-5160",       "default"),
    ("spine", "avery-5160",       "all"),
    ("spine", "avery-5167-spine", "none"),
    ("spine", "avery-5167-spine", "default"),
    ("spine", "avery-5167-spine", "all"),
    ("spine", "avery-5160-spine", "none"),
    ("spine", "avery-5160-spine", "default"),
    ("spine", "avery-5160-spine", "all"),
    ("spine", "avery-22805",      "none"),
    ("spine", "avery-22805",      "default"),
    ("spine", "avery-22805",      "all"),
    ("spine", "avery-22806",      "none"),
    ("spine", "avery-22806",      "default"),
    ("spine", "avery-22806",      "all"),
    # pocket
    ("pocket", "avery-5160",  "none"),
    ("pocket", "avery-5160",  "default"),
    ("pocket", "avery-5160",  "all"),
    ("pocket", "avery-5871",  "none"),
    ("pocket", "avery-5871",  "default"),
    ("pocket", "avery-5871",  "all"),
    ("pocket", "avery-22805", "none"),
    ("pocket", "avery-22805", "default"),
    ("pocket", "avery-22805", "all"),
    ("pocket", "avery-22806", "none"),
    ("pocket", "avery-22806", "default"),
    ("pocket", "avery-22806", "all"),
    # barcode-only
    ("barcode-only", "avery-5160",  "none"),
    ("barcode-only", "avery-5160",  "default"),
    ("barcode-only", "avery-5160",  "all"),
    ("barcode-only", "avery-5167",  "none"),
    ("barcode-only", "avery-5167",  "default"),
    ("barcode-only", "avery-5167",  "all"),
    ("barcode-only", "avery-5871",  "none"),
    ("barcode-only", "avery-5871",  "default"),
    ("barcode-only", "avery-5871",  "all"),
    ("barcode-only", "avery-22805", "none"),
    ("barcode-only", "avery-22805", "default"),
    ("barcode-only", "avery-22805", "all"),
    ("barcode-only", "avery-22806", "none"),
    ("barcode-only", "avery-22806", "default"),
    ("barcode-only", "avery-22806", "all"),
]


@pytest.mark.visual
class TestLabelMatrix:
    """Render all label combinations and verify PNG output."""

    @pytest.fixture(scope="class", autouse=True)
    def render_matrix(self):
        """Run the matrix renderer once per test-class invocation."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(
                f"render_label_matrix.py failed (exit {result.returncode}):\n"
                f"stdout: {result.stdout}\n"
                f"stderr: {result.stderr}"
            )

    @pytest.mark.parametrize("kind,template,fields", EXPECTED_COMBINATIONS)
    def test_png_exists_and_nonempty(self, kind, template, fields):
        """Every expected combination must produce a non-empty PNG."""
        safe_tmpl = template.replace("/", "_")
        path = OUT_DIR / f"{kind}__{safe_tmpl}__{fields}.png"
        assert path.exists(), f"PNG not created: {path.name}"
        assert path.stat().st_size > 500, (
            f"PNG suspiciously small ({path.stat().st_size}B): {path.name}"
        )
