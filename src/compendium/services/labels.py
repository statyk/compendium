"""Label + patron card PDF generation.

Framework-free: takes already-materialized rows, returns bytes. The CLI and
web/API routes are responsible for fetching rows and handing them in.

reportlab is a hard dependency for page layout. ``python-barcode`` supplies
the bar/space module patterns for Codabar / Code 39 / Code 128 (reportlab
doesn't ship a Codabar renderer); we read the patterns and draw rectangles
directly onto the reportlab canvas to avoid a PIL/SVG transitive dep. EAN-13
(used for the optional ISBN-as-barcode flow) still goes through reportlab.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from typing import Iterable, Literal

from reportlab.graphics.barcode import eanbc
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth


BarcodeSymbology = Literal["codabar", "code39", "code128"]


ItemFormat = Literal["spine", "pocket", "barcode-only"]
PatronFormat = Literal["full", "sticker"]

# Minimum label height (inches) that can meaningfully fit a "full" patron card
# (library name + subtitle + name + barcode + expiry). Smaller than this and
# content overlaps — the caller should pick "sticker" format instead.
_FULL_CARD_MIN_HEIGHT = 1.5


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

    @property
    def supports_full_card(self) -> bool:
        """Large enough to render a 'full' patron card without content overlap."""
        return self.label_height >= _FULL_CARD_MIN_HEIGHT


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


_PYBARCODE_CLASS_NAMES: dict[str, str] = {
    "codabar": "codabar",
    "code39": "code39",
    "code128": "code128",
}


def _module_pattern(value: str, symbology: str) -> str:
    """Return the bar/space module pattern for ``value`` under the given
    symbology, as a string of '0' / '1' characters where '1' is a bar.

    ``python-barcode``'s ``Codabar`` class requires the caller to supply
    explicit start/stop characters around the data — we use 'A' on both
    ends, the most common library default. Code 39 and Code 128 don't need
    wrapping. The returned pattern is encoding-only; the human-readable
    text is rendered separately so it shows the unwrapped digits.

    Fallback: Compendium-minted barcodes are always decimal digits, but a
    catalog may carry non-digit barcodes from a legacy import. If the
    chosen symbology can't encode the value (Codabar rejects letters,
    Code 39 rejects most punctuation), we silently fall back to Code 128
    rather than blow up the whole PDF render.
    """
    import barcode

    cls = barcode.get_barcode_class(_PYBARCODE_CLASS_NAMES[symbology])
    encoded = f"A{value}A" if symbology == "codabar" else value
    try:
        return "".join(cls(encoded, writer=None).build())
    except barcode.errors.BarcodeError:
        if symbology == "code128":
            raise  # already at the most permissive symbology — surface it
        fallback = barcode.get_barcode_class("code128")
        return "".join(fallback(value, writer=None).build())


def _human_readable_text(value: str, symbology: str) -> str:
    """Text printed below the bars. Compendium's barcode value is the same
    across symbologies — Codabar's start/stop chars are an encoding detail,
    not user-visible — so this is a thin wrapper for symmetry with
    ``_module_pattern`` and to keep symbology-specific stripping in one
    place if more symbologies arrive later."""
    return value


def _draw_barcode(
    c: canvas.Canvas,
    x: float,
    y: float,
    value: str,
    width: float,
    height: float,
    *,
    symbology: BarcodeSymbology,
    human_readable: bool = True,
) -> None:
    """Render a barcode for ``value`` at the canvas position ``(x, y)``,
    using the requested symbology. Bars and spaces are emitted as
    rectangles directly onto the canvas — no PIL / SVG dep, no reportlab
    barcode widget."""
    pattern = _module_pattern(value, symbology)
    if not pattern:
        return
    module_width = width / len(pattern)
    c.saveState()
    for i, bit in enumerate(pattern):
        if bit == "1":
            c.rect(x + i * module_width, y, module_width, height,
                   fill=1, stroke=0)
    if human_readable:
        c.setFont("Helvetica", 7)
        c.drawCentredString(
            x + width / 2.0,
            y - 8,
            _human_readable_text(value, symbology),
        )
    c.restoreState()


def _draw_barcode_ean13(
    c: canvas.Canvas,
    x: float,
    y: float,
    isbn: str,
    width: float,
    height: float,
    *,
    fallback_symbology: BarcodeSymbology,
    human_readable: bool = True,
) -> None:
    # Strip non-digits; Ean13BarcodeWidget validates length and check digit.
    digits = "".join(ch for ch in isbn if ch.isdigit())
    if len(digits) == 13:
        value = digits
    elif len(digits) == 12:
        value = digits  # widget will compute check digit
    else:
        # Malformed ISBN — fall back to the operator's chosen symbology so
        # their scanner can still read whatever digits we encode.
        _draw_barcode(
            c, x, y, isbn, width, height,
            symbology=fallback_symbology,
            human_readable=human_readable,
        )
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

    ``format`` defaults based on template size:
      - narrow templates (≤2") → 'barcode-only' (just a scannable barcode +
        human-readable number; good for small stickers affixed to spines/pockets)
      - larger templates → 'pocket' (title + call number + cutter/year + barcode)
    Caller may override. 'spine' format is text-only (no barcode), matching
    traditional shelving-label convention.

    ``use_isbn_barcode`` makes the generator draw an EAN-13 for rows that
    carry a valid ISBN; falls back to the configured symbology over the
    internal barcode.

    Symbology (Codabar / Code 39 / Code 128) is read from the
    ``barcode_symbology`` site setting, not threaded as a parameter — it's
    a "set once for your scanner hardware" decision, not a per-render one.
    """
    from compendium.services.site_settings import get_site_setting

    symbology: BarcodeSymbology = get_site_setting("barcode_symbology")

    template = TEMPLATES[template_key]
    if format is None:
        format = "barcode-only" if template.label_width <= 2.0 else "pocket"

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(template.page_width * inch, template.page_height * inch))
    items_list = list(items)
    positions = _iter_label_positions(template, start_label)
    current_page = 0

    for row, (_, x, y, page_idx) in zip(items_list, positions):
        if page_idx != current_page:
            c.showPage()
            current_page = page_idx
        _draw_item_label(c, row, x, y, template, format, use_isbn_barcode, symbology)

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
    symbology: BarcodeSymbology,
) -> None:
    lw = t.label_width * inch
    lh = t.label_height * inch
    pad = 4  # points
    inner_w = lw - 2 * pad

    font = "Helvetica-Bold"
    body_font = "Helvetica"

    cn_lines = wrap_call_number(row.call_number or "", max_chars=10)
    cutter_str = cutter(row.author_display)
    year = str(row.publication_year) if row.publication_year else ""

    if fmt == "barcode-only":
        # Barcode fills most of the label; human-readable digits sit beneath.
        bc_h = max(8.0, lh - 2 * pad - 2)
        if use_isbn and row.isbn:
            _draw_barcode_ean13(
                c, x + pad, y + pad, row.isbn, inner_w, bc_h,
                fallback_symbology=symbology,
            )
        else:
            _draw_barcode(
                c, x + pad, y + pad, row.barcode, inner_w, bc_h,
                symbology=symbology,
            )
        return

    if fmt == "spine":
        # Fixed geometry so a missing call number doesn't shift the cutter/year
        # up (caller complaint: inconsistent placement across a batch).
        # Reserve space for: up to 4 call-number lines, cutter line, year line.
        cn_font_size = 9
        cutter_font_size = 10
        year_font_size = 9
        line_h_cn = cn_font_size + 1
        # Call number block: top of label, up to 4 lines.
        top = y + lh - pad
        max_cn_lines = 4
        cn_slots = min(len(cn_lines), max_cn_lines)
        # Walk down from the top: always reserve 4 line-heights so missing
        # lines leave blank space rather than letting cutter/year float up.
        cursor = top - cn_font_size
        c.setFont(font, cn_font_size)
        for i in range(max_cn_lines):
            if i < cn_slots:
                c.drawString(x + pad, cursor, _truncate(cn_lines[i], inner_w, font, cn_font_size))
            cursor -= line_h_cn
        # Cutter (bold) + year (regular) on their own lines below the block.
        if cutter_str:
            c.setFont(font, cutter_font_size)
            c.drawString(x + pad, cursor - 2, cutter_str)
        cursor -= cutter_font_size + 2
        if year:
            c.setFont(body_font, year_font_size)
            c.drawString(x + pad, cursor - 2, year)
        return

    # pocket format — rework for better space use:
    #   top row:  title (full width), small
    #   middle:   call number joined with slashes + cutter + year (one line)
    #   bottom:   barcode (full width), digits underneath
    # Reserve space for the middle line even when call number is empty, so
    # labels in a batch keep consistent geometry.
    title_size = 8
    info_size = 9
    top_y = y + lh - pad - title_size
    mid_y = top_y - title_size - 4

    # Title (top, full inner width)
    if row.title:
        c.setFont(body_font, title_size)
        title_text = row.title
        if row.author_display:
            title_text = f"{row.title} — {row.author_display}"
        c.drawString(x + pad, top_y, _truncate(title_text, inner_w, body_font, title_size))

    # Call-number line: "PS3551 / .E76 / D8 / 1965 · HER" style
    parts: list[str] = []
    if cn_lines:
        parts.append(" ".join(cn_lines))  # e.g. "PS3551 .E76 D8 1965"
    if cutter_str:
        parts.append(cutter_str)
    if year and not cn_lines:
        # If the year wasn't already in the call number, append it.
        parts.append(year)
    info_text = "  ·  ".join(parts) if parts else ""
    c.setFont(font, info_size)
    c.drawString(x + pad, mid_y, _truncate(info_text, inner_w, font, info_size))

    # Barcode at the bottom
    bc_h = 20
    bc_y = y + pad
    if use_isbn and row.isbn:
        _draw_barcode_ean13(
            c, x + pad, bc_y, row.isbn, inner_w, bc_h,
            fallback_symbology=symbology,
        )
    else:
        _draw_barcode(
            c, x + pad, bc_y, row.barcode, inner_w, bc_h,
            symbology=symbology,
        )


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

    Raises ``ValueError`` if ``full`` is requested on a template too small to
    render it without content overlap — use ``sticker`` instead.
    """
    from compendium.services.site_settings import get_site_setting

    symbology: BarcodeSymbology = get_site_setting("barcode_symbology")

    template = TEMPLATES[template_key]
    if format == "full" and not template.supports_full_card:
        raise ValueError(
            f"Template '{template_key}' is too small for 'full' format "
            f"(label height {template.label_height}\" < {_FULL_CARD_MIN_HEIGHT}\"). "
            f"Use 'sticker' format on this template, or pick a larger template "
            f"such as 'avery-5871' or 'avery-5390'."
        )
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
            _draw_patron_full(c, row, x, y, template, library_name, symbology)
        else:
            _draw_patron_sticker(c, row, x, y, template, symbology)

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
    symbology: BarcodeSymbology,
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
    _draw_barcode(
        c, x + pad, bc_y, row.card_number, bc_w, bc_h,
        symbology=symbology, human_readable=True,
    )

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
    symbology: BarcodeSymbology,
) -> None:
    lw = t.label_width * inch
    lh = t.label_height * inch
    pad = 2
    inner_w = lw - 2 * pad

    # Barcode fills most of the label; human-readable text below bars.
    bc_h = lh - 2 * pad - 2
    _draw_barcode(
        c, x + pad, y + pad, row.card_number, inner_w, bc_h,
        symbology=symbology, human_readable=True,
    )
