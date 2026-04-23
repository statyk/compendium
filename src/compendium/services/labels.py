"""Label + patron card PDF generation.

Framework-free: takes already-materialized rows, returns bytes. The CLI and
web/API routes are responsible for fetching rows and handing them in.

reportlab is a hard dependency. `reportlab.graphics.barcode` provides the
Code128 and EAN-13 renderers we need, so there's no additional barcode lib.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from typing import Iterable, Literal

from reportlab.graphics.barcode import code128, eanbc
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth


ItemFormat = Literal["spine", "pocket"]
PatronFormat = Literal["full", "sticker"]


@dataclass
class LabelTemplate:
    """Dimensions for a pre-cut label sheet. All units are inches."""

    key: str
    display: str
    page_width: float = 8.5
    page_height: float = 11.0
    cols: int = 3
    rows: int = 10
    label_width: float = 2.625
    label_height: float = 1.0
    margin_left: float = 0.1875
    margin_top: float = 0.5
    col_gap: float = 0.125
    row_gap: float = 0.0

    @property
    def per_sheet(self) -> int:
        return self.cols * self.rows


# Generic pre-cut label sheet geometries. Template keys reference the common
# Avery numbers users will be buying; UI labels read dimensions too.
TEMPLATES: dict[str, LabelTemplate] = {
    "avery-5160": LabelTemplate(
        key="avery-5160",
        display="Address / spine — 3×10, 1\" × 2⅝\" (Avery 5160/5260)",
        cols=3, rows=10,
        label_width=2.625, label_height=1.0,
        margin_left=0.1875, margin_top=0.5,
        col_gap=0.125, row_gap=0.0,
    ),
    "avery-5167": LabelTemplate(
        key="avery-5167",
        display="Return-address — 4×20, ½\" × 1¾\" (Avery 5167)",
        cols=4, rows=20,
        label_width=1.75, label_height=0.5,
        margin_left=0.30, margin_top=0.5,
        col_gap=0.30, row_gap=0.0,
    ),
    "avery-5871": LabelTemplate(
        key="avery-5871",
        display="Business card — 2×5, 2\" × 3½\" (Avery 5871/8371)",
        cols=2, rows=5,
        label_width=3.5, label_height=2.0,
        margin_left=0.75, margin_top=0.5,
        col_gap=0.0, row_gap=0.0,
    ),
    "avery-5390": LabelTemplate(
        key="avery-5390",
        display="Name badge — 4 columns × 2 rows, 3½\" × 2¼\" (Avery 5390 variant)",
        cols=2, rows=4,
        label_width=3.5, label_height=2.25,
        margin_left=0.75, margin_top=0.5,
        col_gap=0.0, row_gap=0.0,
    ),
}


# ──────────────────────────────────────────────────────────────────────
# Data rows — framework-free DTOs the service renders. Callers build these
# from DB rows (so the service doesn't reach back through the ORM).
# ──────────────────────────────────────────────────────────────────────


@dataclass
class ItemLabelRow:
    barcode: str
    title: str
    author_display: str | None = None  # e.g. "Frank Herbert"; full form, we cutter it
    call_number: str | None = None
    publication_year: int | None = None
    isbn: str | None = None  # if present and use_isbn_barcode=True, EAN-13 is drawn
    branch_code: str | None = None


@dataclass
class PatronCardRow:
    card_number: str
    full_name: str
    expires_at: date | None = None


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def cutter(author_display: str | None) -> str:
    """3-letter uppercase author surname indicator (e.g. 'Herbert' → 'HER').

    Handles 'Lastname, Firstname' form. Returns '' for empty/None. Not a true
    Cutter-Sanborn number — good enough for stack browsing."""
    if not author_display or not author_display.strip():
        return ""
    name = author_display.strip()
    if "," in name:
        # "Herbert, Frank" → surname is the part before the comma
        surname = name.split(",", 1)[0].strip()
    else:
        # "Frank Herbert" → surname is the last whitespace-separated token
        parts = name.split()
        surname = parts[-1] if parts else ""
    # Strip non-letters (handle "O'Brien" → "OBR", etc.)
    surname = re.sub(r"[^A-Za-z]", "", surname)
    return surname[:3].upper()


def wrap_call_number(cn: str, max_chars: int = 10) -> list[str]:
    """Split a call number across multiple lines at natural break points for
    stacked display. LCC: 'PS3551 .E76 D8 1965' → ['PS3551', '.E76', 'D8', '1965'].
    DDC: '823.912 HER' → ['823.912', 'HER']. Falls back to hard-wrap at max_chars."""
    if not cn:
        return []
    # First try splitting on whitespace — that gets LCC and DDC cutter/year pieces cleanly.
    pieces = cn.strip().split()
    # Any piece still too long? hard-wrap it.
    result: list[str] = []
    for p in pieces:
        while len(p) > max_chars:
            result.append(p[:max_chars])
            p = p[max_chars:]
        if p:
            result.append(p)
    return result


def _truncate(s: str, max_w: float, font_name: str, font_size: int) -> str:
    """Truncate a string with an ellipsis so its rendered width fits max_w (points)."""
    if stringWidth(s, font_name, font_size) <= max_w:
        return s
    ell = "…"
    while s and stringWidth(s + ell, font_name, font_size) > max_w:
        s = s[:-1]
    return (s + ell) if s else ""


# ──────────────────────────────────────────────────────────────────────
# Layout — the canvas loop that's shared between items and patrons
# ──────────────────────────────────────────────────────────────────────


def _iter_label_positions(template: LabelTemplate, start_label: int = 0):
    """Yield (label_index, origin_x_pts, origin_y_pts) in reading order.
    Origin is the lower-left corner of each label in PDF points."""
    pw = template.page_width * inch
    ph = template.page_height * inch
    lw = template.label_width * inch
    lh = template.label_height * inch
    ml = template.margin_left * inch
    mt = template.margin_top * inch
    cg = template.col_gap * inch
    rg = template.row_gap * inch
    skip = max(0, start_label)
    idx = 0
    page_idx = 0
    while True:
        for r in range(template.rows):
            for c in range(template.cols):
                if idx < skip:
                    idx += 1
                    continue
                x = ml + c * (lw + cg)
                # PDF origin is bottom-left; page_idx scrolls rows from top.
                y = ph - mt - (r + 1) * lh - r * rg
                yield (idx - skip, x, y, page_idx)
                idx += 1
        page_idx += 1


def _draw_barcode_code128(c: canvas.Canvas, x: float, y: float, value: str,
                          width: float, height: float, human_readable: bool = True) -> None:
    bc = code128.Code128(value, barHeight=height, humanReadable=human_readable,
                         fontName="Helvetica", fontSize=7)
    # reportlab barcode widgets have their own coordinate system; scale the
    # drawn width to the allotted space.
    natural = bc.width
    scale = width / natural if natural > 0 else 1.0
    c.saveState()
    c.translate(x, y)
    c.scale(scale, 1.0)
    bc.drawOn(c, 0, 0)
    c.restoreState()


def _draw_barcode_ean13(c: canvas.Canvas, x: float, y: float, isbn: str,
                        width: float, height: float, human_readable: bool = True) -> None:
    # Strip non-digits; Ean13BarcodeWidget validates length and check digit.
    digits = "".join(ch for ch in isbn if ch.isdigit())
    if len(digits) == 13:
        value = digits
    elif len(digits) == 12:
        value = digits  # widget will compute check digit
    else:
        # fall back to Code128 if ISBN is malformed
        _draw_barcode_code128(c, x, y, isbn, width, height, human_readable)
        return
    bw = eanbc.Ean13BarcodeWidget(value, humanReadable=human_readable,
                                   fontName="Helvetica", fontSize=7, barHeight=height)
    drawing = Drawing(width, height)
    drawing.add(bw)
    # Scale to the requested width
    nb = bw.width
    scale = width / nb if nb > 0 else 1.0
    drawing.scale(scale, 1.0)
    drawing.drawOn(c, x, y)


# ──────────────────────────────────────────────────────────────────────
# Item labels
# ──────────────────────────────────────────────────────────────────────


def generate_item_labels(
    items: Iterable[ItemLabelRow],
    *,
    template_key: str = "avery-5160",
    format: ItemFormat | None = None,
    use_isbn_barcode: bool = False,
    start_label: int = 0,
) -> bytes:
    """Render item labels to PDF bytes.

    ``format`` defaults based on template size: small templates → 'spine'
    (call number + cutter + year, no barcode), larger templates → 'pocket'
    (adds title and barcode). Caller may override.

    ``use_isbn_barcode`` makes the generator draw an EAN-13 for rows that
    carry a valid ISBN; falls back to Code128 over the internal barcode.
    """
    template = TEMPLATES[template_key]
    if format is None:
        format = "spine" if template.label_width <= 2.0 else "pocket"

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(template.page_width * inch, template.page_height * inch))
    items_list = list(items)
    positions = _iter_label_positions(template, start_label)
    current_page = 0

    for row, (_, x, y, page_idx) in zip(items_list, positions):
        if page_idx != current_page:
            c.showPage()
            current_page = page_idx
        _draw_item_label(c, row, x, y, template, format, use_isbn_barcode)

    c.showPage()
    c.save()
    return buf.getvalue()


def _draw_item_label(
    c: canvas.Canvas,
    row: ItemLabelRow,
    x: float,
    y: float,
    t: LabelTemplate,
    fmt: ItemFormat,
    use_isbn: bool,
) -> None:
    lw = t.label_width * inch
    lh = t.label_height * inch
    pad = 4  # points
    inner_w = lw - 2 * pad

    font = "Helvetica-Bold"
    body_font = "Helvetica"

    # Always: stacked call number at the top, left-aligned
    cn_lines = wrap_call_number(row.call_number or "", max_chars=10)
    cutter_str = cutter(row.author_display)
    year = str(row.publication_year) if row.publication_year else ""

    # Build the stack
    stack = list(cn_lines)
    if cutter_str:
        stack.append(cutter_str)
    if year:
        stack.append(year)

    if fmt == "spine":
        # Spine: just the stacked text, no barcode, no title.
        # Size the font so the stack fits vertically.
        cn_font_size = 9 if len(stack) <= 4 else 7
        line_h = cn_font_size + 1
        cursor_y = y + lh - pad - cn_font_size
        for line in stack:
            text = _truncate(line, inner_w, font, cn_font_size)
            c.setFont(font, cn_font_size)
            c.drawString(x + pad, cursor_y, text)
            cursor_y -= line_h
            if cursor_y < y + pad:
                break
        return

    # pocket format: call number top-left, title small, barcode+number below.
    cn_font_size = 9
    line_h = cn_font_size + 1
    cursor_y = y + lh - pad - cn_font_size
    for line in stack:
        if cursor_y < y + pad + 22:  # leave room for barcode
            break
        text = _truncate(line, inner_w * 0.55, font, cn_font_size)
        c.setFont(font, cn_font_size)
        c.drawString(x + pad, cursor_y, text)
        cursor_y -= line_h

    # Title on the right of the call number block
    if row.title:
        title = _truncate(row.title, inner_w * 0.55, body_font, 7)
        c.setFont(body_font, 7)
        c.drawString(x + pad + inner_w * 0.45, y + lh - pad - 7, title)

    # Barcode + number at the bottom
    bc_h = 18
    bc_y = y + pad
    if use_isbn and row.isbn:
        _draw_barcode_ean13(c, x + pad, bc_y, row.isbn, inner_w, bc_h)
    else:
        _draw_barcode_code128(c, x + pad, bc_y, row.barcode, inner_w, bc_h)


# ──────────────────────────────────────────────────────────────────────
# Patron cards
# ──────────────────────────────────────────────────────────────────────


def generate_patron_cards(
    patrons: Iterable[PatronCardRow],
    *,
    template_key: str = "avery-5871",
    format: PatronFormat = "full",
    library_name: str = "Compendium",
    start_label: int = 0,
) -> bytes:
    """Render patron cards to PDF bytes.

    ``full`` mode (for 5871/5390 cardstock): library name header + patron name +
    card number + barcode + expiry.
    ``sticker`` mode (for 5160/5167 small labels): card number + barcode only,
    intended to be affixed to a pre-made card the library ordered separately.
    """
    template = TEMPLATES[template_key]
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(template.page_width * inch, template.page_height * inch))
    patrons_list = list(patrons)
    positions = _iter_label_positions(template, start_label)
    current_page = 0

    for row, (_, x, y, page_idx) in zip(patrons_list, positions):
        if page_idx != current_page:
            c.showPage()
            current_page = page_idx
        if format == "full":
            _draw_patron_full(c, row, x, y, template, library_name)
        else:
            _draw_patron_sticker(c, row, x, y, template)

    c.showPage()
    c.save()
    return buf.getvalue()


def _draw_patron_full(
    c: canvas.Canvas,
    row: PatronCardRow,
    x: float,
    y: float,
    t: LabelTemplate,
    library_name: str,
) -> None:
    lw = t.label_width * inch
    lh = t.label_height * inch
    pad = 8
    inner_w = lw - 2 * pad

    # Library name header
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(x + lw / 2, y + lh - pad - 10,
                        _truncate(library_name, inner_w, "Helvetica-Bold", 10))
    c.setFont("Helvetica", 8)
    c.drawCentredString(x + lw / 2, y + lh - pad - 22, "Library Card")

    # Patron name
    c.setFont("Helvetica", 11)
    c.drawCentredString(x + lw / 2, y + lh - pad - 44,
                        _truncate(row.full_name, inner_w, "Helvetica", 11))

    # Barcode + card number
    bc_h = 28
    bc_y = y + pad + 12
    bc_w = inner_w
    _draw_barcode_code128(c, x + pad, bc_y, row.card_number, bc_w, bc_h, human_readable=True)

    # Expiry (bottom-right corner)
    if row.expires_at:
        c.setFont("Helvetica", 7)
        c.drawRightString(x + lw - pad, y + pad,
                          f"Expires {row.expires_at.isoformat()}")


def _draw_patron_sticker(
    c: canvas.Canvas,
    row: PatronCardRow,
    x: float,
    y: float,
    t: LabelTemplate,
) -> None:
    lw = t.label_width * inch
    lh = t.label_height * inch
    pad = 2
    inner_w = lw - 2 * pad

    # Barcode fills most of the label; human-readable text below bars.
    bc_h = lh - 2 * pad - 2
    _draw_barcode_code128(c, x + pad, y + pad, row.card_number,
                          inner_w, bc_h, human_readable=True)
