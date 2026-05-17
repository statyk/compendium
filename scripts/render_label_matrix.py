#!/usr/bin/env python3
"""
Render every (kind × template × field-set) label combination and save PNGs.

Usage (from the project root):
    uv run --extra e2e python scripts/render_label_matrix.py

Output:
    out/labels/<kind>__<template>__<fields>.png

This is an agent/developer tool — not a test. Run it when you want to
visually verify label layouts after making changes to the spine/pocket
rendering code. Claude can then `Read` each PNG to inspect the output.

Environment:
    COMPENDIUM_MATRIX_ADMIN_USER   (default: matrix_admin)
    COMPENDIUM_MATRIX_ADMIN_PASS   (default: matrix-secret-1)
    COMPENDIUM_DATABASE_URL        (default: sqlite:////tmp/label_matrix.db)
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent
OUT_DIR = PROJECT_ROOT / "out" / "labels"
ADMIN_USER = os.environ.get("COMPENDIUM_MATRIX_ADMIN_USER", "matrix_admin")
ADMIN_PASS = os.environ.get("COMPENDIUM_MATRIX_ADMIN_PASS", "matrix-secret-1")
DEFAULT_DB = "sqlite:////tmp/label_matrix.db"
DB_URL = os.environ.get("COMPENDIUM_DATABASE_URL", DEFAULT_DB)

ITEM_KINDS = ["spine", "pocket", "barcode-only"]

# Per-kind field sets: (label, frozenset of field names to enable)
FIELD_SETS: dict[str, list[tuple[str, frozenset[str]]]] = {
    "spine": [
        ("none", frozenset()),
        ("default", frozenset({"call_number", "location", "cutter", "year"})),
        ("all", frozenset({"branch", "location", "call_number", "cutter", "year", "barcode"})),
    ],
    "pocket": [
        ("none", frozenset()),
        ("default", frozenset({"barcode", "title", "author", "call_number", "cutter", "year"})),
        ("all", frozenset({"title", "author", "call_number", "barcode", "cutter", "year",
                           "branch", "library_name"})),
    ],
    "barcode-only": [
        ("none", frozenset()),
        ("default", frozenset({"barcode", "human_readable"})),
        ("all", frozenset({"barcode", "title", "human_readable"})),
    ],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _run(cmd: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(cmd, env={**os.environ, **env}, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")


def _wait_for_server(base_url: str, timeout: int = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/ui/login", timeout=1)
            return
        except Exception:
            time.sleep(0.3)
    raise TimeoutError(f"Server at {base_url} not ready after {timeout}s")


def _compatible_templates(kind: str) -> list[str]:
    """Import and call the service function to get template keys for a kind."""
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from compendium.services.labels import compatible_templates
    return [t.key for t in compatible_templates(kind)]


def _field_params(fields: frozenset[str]) -> dict[str, str]:
    return {f"field_{f}": "" for f in fields}


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    env = {
        "COMPENDIUM_DATABASE_URL": DB_URL,
        "COMPENDIUM_ALLOW_INSECURE_JWT": "1",
    }

    print("Initialising database …")
    _run(["uv", "run", "compendium", "db", "init"], env)

    print(f"Creating admin user '{ADMIN_USER}' …")
    _run([
        "uv", "run", "compendium", "user", "add",
        "--username", ADMIN_USER,
        "--password", ADMIN_PASS,
        "--role", "Administrator",
        "--allow-bootstrap",
    ], env)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        ["uv", "run", "compendium", "serve", "--port", str(port)],
        env={**os.environ, **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        print(f"Waiting for server on port {port} …")
        _wait_for_server(base_url)
        print("Server ready. Launching Playwright …")
        _render_all(base_url)
    finally:
        server.terminate()
        server.wait(timeout=5)
        print("Server stopped.")


def _render_all(base_url: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})

        # Log in
        page.goto(f"{base_url}/ui/login")
        page.fill("input[name='username']", ADMIN_USER)
        page.fill("input[name='password']", ADMIN_PASS)
        page.click("button[type='submit']")
        page.wait_for_url(f"{base_url}/ui/**")

        total = 0
        for kind in ITEM_KINDS:
            templates = _compatible_templates(kind)
            for tmpl_key in templates:
                for fields_label, fields in FIELD_SETS.get(kind, []):
                    params: dict[str, str] = {"kind": kind, "template": tmpl_key}
                    params.update(_field_params(fields))
                    qs = "&".join(f"{k}={v}" for k, v in params.items())
                    url = f"{base_url}/ui/labels/items/preview?{qs}"

                    safe_tmpl = tmpl_key.replace("/", "_")
                    out_path = OUT_DIR / f"{kind}__{safe_tmpl}__{fields_label}.png"

                    page.goto(url)
                    # The preview partial returns .label-preview-box containing the SVG.
                    page.wait_for_selector(".label-preview-box", timeout=8000)
                    el = page.query_selector(".label-preview-box")
                    if el:
                        el.screenshot(path=str(out_path))
                        print(f"  ✓ {out_path.name}")
                        total += 1
                    else:
                        print(f"  ✗ no .label-preview-box for {kind}/{tmpl_key}/{fields_label}",
                              file=sys.stderr)

        browser.close()
        print(f"\nDone — {total} PNGs written to {OUT_DIR}")


if __name__ == "__main__":
    main()
