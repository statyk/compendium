"""
Tests for the vendored QR encoder (compendium.web.qrcode).

Correctness strategy
--------------------
We cannot round-trip decode without adding a decoder dependency, so the
tests are layered from most to least strict:

1. **Structural invariants** (no external dep) — finder patterns at all
   three corners, quiet zone, matrix dimension formula, version selection.
2. **Known-answer from vendored source** — Project Nayuki's own library
   ships a ``qrcodegen-demo.py`` that exercises "Hello, world!" at LOW and
   asserts a specific printed output; the *size* of that code is fixed
   (version 1 → size 21).  We verify the dimension and that the three
   7×7 finder pattern corners are dark exactly as the spec requires.
3. **Determinism** — same input produces identical SVG on repeated calls.
4. **SVG validity** — parses as XML; root is ``<svg>``; ``viewBox`` covers
   the full symbol + border.
5. **Realistic pairing-URL** — a ~118-char https URL encodes successfully
   and selects a version large enough for that payload at EC M.
"""

import re
import xml.etree.ElementTree as ET

import pytest

from compendium.web.qrcode import _EC_MAP, _QrCode, qr_svg

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_qr(text: str, ec: str = "M") -> _QrCode:
    """Build a _QrCode object directly for structural inspection."""
    return _QrCode.encode_text(text, _EC_MAP[ec])


def _finder_dark_at(qr: _QrCode, cx: int, cy: int) -> bool:
    """
    Check that the 7×7 finder pattern centred at (cx, cy) is correctly
    drawn.  The outer ring and inner 3×3 core must be dark; the ring at
    distance 2 must be light.
    """
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            expected = max(abs(dx), abs(dy)) != 2  # dark except the separator ring
            if qr.get_module(cx + dx, cy + dy) != expected:
                return False
    return True


# ---------------------------------------------------------------------------
# 1. Dimension invariant: size = 4*version + 17
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,ec", [
    ("1", "L"),
    ("Hello, world!", "M"),
    ("A" * 50, "M"),
    ("A" * 100, "M"),
])
def test_size_formula(text: str, ec: str) -> None:
    qr = _make_qr(text, ec)
    assert qr.get_size() == 4 * qr.get_version() + 17


# ---------------------------------------------------------------------------
# 2. Three finder patterns at the three mandatory corners
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["Hello, world!", "https://example.org/scan?c=TOKEN"])
def test_finder_patterns_present(text: str) -> None:
    qr = _make_qr(text)
    size = qr.get_size()
    # Top-left finder centre is at (3, 3)
    assert _finder_dark_at(qr, 3, 3), "top-left finder pattern wrong"
    # Top-right finder centre is at (size-4, 3)
    assert _finder_dark_at(qr, size - 4, 3), "top-right finder pattern wrong"
    # Bottom-left finder centre is at (3, size-4)
    assert _finder_dark_at(qr, 3, size - 4), "bottom-left finder pattern wrong"


# ---------------------------------------------------------------------------
# 3. Quiet zone: border modules must be all light
# ---------------------------------------------------------------------------


def test_quiet_zone_in_svg() -> None:
    """The SVG path must not place any module inside the quiet zone."""
    border = 4
    svg = qr_svg("https://example.org/", border=border)
    root = ET.fromstring(svg)
    path_el = root.find("{http://www.w3.org/2000/svg}path")
    assert path_el is not None
    path_d = path_el.get("d", "")
    # Every "M x,y h1v1h-1z" rect starts at (x, y); x and y must be >= border.
    for m in re.finditer(r"M(\d+),(\d+)", path_d):
        x, y = int(m.group(1)), int(m.group(2))
        assert x >= border, f"module at x={x} is inside the quiet zone (border={border})"
        assert y >= border, f"module at y={y} is inside the quiet zone (border={border})"


# ---------------------------------------------------------------------------
# 4. Version selection for ~120-char payload at EC M
# ---------------------------------------------------------------------------


PAIRING_URL = (
    "https://library.example.org/ui/scan/pair?c=AbCdEfGhIjKlMnOpQrStUvWxYz012345"
)  # 80-char token URL — selects version 5 or above at EC M


def test_version_for_pairing_url() -> None:
    qr = _make_qr(PAIRING_URL, "M")
    # Version 4 capacity at EC M: 62 bytes; version 5: 77 bytes; version 6: 134.
    # PAIRING_URL is ~80 bytes UTF-8, so the smallest fit is version 5.
    assert qr.get_version() >= 5, "version too small for pairing URL"
    assert qr.get_version() <= 10, "version unexpectedly large (sanity cap)"


def test_120_char_url_selects_sane_version() -> None:
    # 118-char URL representing worst-case pairing URL
    url = "https://library.example.org/ui/scan/pair?c=" + "A" * 74
    assert len(url) <= 120
    qr = _make_qr(url, "M")
    assert qr.get_version() >= 5
    assert qr.get_version() <= 15  # generous sanity cap


# ---------------------------------------------------------------------------
# 5. Known-answer: "Hello, world!" at LOW is version 1 (size 21)
# ---------------------------------------------------------------------------


def test_hello_world_version_1() -> None:
    """
    Nayuki's own demo encodes "Hello, world!" at LOW and prints it; the
    library's docs confirm it picks version 1.  Version 1 has size 21.
    """
    qr = _make_qr("Hello, world!", "L")
    assert qr.get_version() == 1
    assert qr.get_size() == 21


# ---------------------------------------------------------------------------
# 6. Determinism
# ---------------------------------------------------------------------------


def test_determinism() -> None:
    url = "https://library.example.org/ui/scan/pair?c=TestToken12345"
    first = qr_svg(url)
    second = qr_svg(url)
    assert first == second


# ---------------------------------------------------------------------------
# 7. SVG validity: parses as XML, root is <svg>, viewBox covers full symbol
# ---------------------------------------------------------------------------


def test_svg_valid_xml_and_viewbox() -> None:
    border = 4
    svg = qr_svg("https://example.org/test", border=border)
    root = ET.fromstring(svg)
    # Root tag (may be namespaced)
    assert root.tag in ("svg", "{http://www.w3.org/2000/svg}svg")
    viewbox = root.get("viewBox", "")
    parts = viewbox.split()
    assert len(parts) == 4, f"viewBox should have 4 values, got: {viewbox!r}"
    vb_x, vb_y, vb_w, vb_h = (int(p) for p in parts)
    assert vb_x == 0 and vb_y == 0
    # width and height must equal size + 2*border
    qr = _make_qr("https://example.org/test")
    expected = qr.get_size() + 2 * border
    assert vb_w == expected
    assert vb_h == expected


def test_svg_custom_border() -> None:
    svg2 = qr_svg("HELLO", border=2)
    svg6 = qr_svg("HELLO", border=6)
    root2 = ET.fromstring(svg2)
    root6 = ET.fromstring(svg6)
    vb2 = [int(v) for v in root2.get("viewBox", "").split()]
    vb6 = [int(v) for v in root6.get("viewBox", "").split()]
    # Both encode same data; outer dimension differs by 2*(6-2)=8
    assert vb6[2] - vb2[2] == 8
    assert vb6[3] - vb2[3] == 8


# ---------------------------------------------------------------------------
# 8. Error handling
# ---------------------------------------------------------------------------


def test_negative_border_raises() -> None:
    with pytest.raises(ValueError, match="border"):
        qr_svg("data", border=-1)


def test_bad_ec_level_raises() -> None:
    with pytest.raises(ValueError, match="error_correction"):
        qr_svg("data", error_correction="X")


def test_ec_levels_all_work() -> None:
    for level in ("L", "M", "Q", "H"):
        svg = qr_svg("https://example.org/", error_correction=level)
        ET.fromstring(svg)  # must parse without error


# ---------------------------------------------------------------------------
# 9. SVG output is inline (no DOCTYPE, no external refs)
# ---------------------------------------------------------------------------


def test_svg_is_inline() -> None:
    svg = qr_svg("https://example.org/")
    assert "<!DOCTYPE" not in svg
    assert "http://www.w3.org/Graphics/SVG" not in svg
    assert svg.startswith("<svg ")
    assert svg.endswith("</svg>")
