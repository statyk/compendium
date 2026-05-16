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
    orientation: str = "landscape"

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
        display="Return-address / pocket label — 4×20, ½\" × 1¾\" (Avery 5167/8167)",
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
    "avery-5167-spine": LabelTemplate(
        key="avery-5167-spine",
        display="Spine label — 4×20, ½\" × 1¾\" rotated (Avery 5167/8167)",
        cols=4, rows=20,
        label_width=1.75, label_height=0.5,
        margin_left=0.30, margin_top=0.5,
        col_gap=0.30, row_gap=0.0,
        orientation="rotated",
    ),
    "avery-22805": LabelTemplate(
        key="avery-22805",
        display="Square — 4×6, 1½\" × 1½\" (Avery 22805)",
        cols=4, rows=6,
        label_width=1.5, label_height=1.5,
        margin_left=0.5, margin_top=1.0,
        col_gap=0.5, row_gap=0.0,
    ),
    "avery-22806": LabelTemplate(
        key="avery-22806",
        display="Square — 3×4, 2\" × 2\" (Avery 22806)",
        cols=3, rows=4,
        label_width=2.0, label_height=2.0,
        margin_left=1.25, margin_top=1.5,
        col_gap=0.5, row_gap=0.0,
    ),
}


# ──────────────────────────────────────────────────────────────────────
# Label-kind taxonomy
#
# "Kind" is the user-facing name for the label format. Web forms and CLI
# subcommands use the kind vocabulary; these dicts translate to the
# underlying ItemFormat / PatronFormat used by the rendering helpers.
# ──────────────────────────────────────────────────────────────────────

ITEM_KIND_TO_FORMAT: dict[str, str] = {
    "spine":        "spine",
    "pocket":       "pocket",
    "barcode-only": "barcode-only",
}

PATRON_KIND_TO_FORMAT: dict[str, str] = {
    "patron-full":    "full",
    "patron-sticker": "sticker",
}

KIND_DEFAULT_TEMPLATE: dict[str, str] = {
    "spine":          "avery-5167-spine",
    "pocket":         "avery-5160",
    "barcode-only":   "avery-5167",
    "patron-full":    "avery-5871",
    "patron-sticker": "avery-5167",
}

# Every field is optional — callers choose what to draw; DEFAULT_FIELDS provides
# sensible on-by-default choices that preserve existing visual behaviour.
OPTIONAL_FIELDS: dict[str, frozenset[str]] = {
    "spine":        frozenset({"call_number", "barcode", "location", "branch", "cutter", "year"}),
    "pocket":       frozenset({"title", "author", "call_number", "barcode", "cutter", "year", "branch", "library_name"}),
    "barcode-only": frozenset({"barcode", "title", "human_readable"}),
    "full":         frozenset({"barcode", "card_number", "library_name", "subtitle", "patron_name", "expiry", "category"}),
    "sticker":      frozenset({"barcode", "card_number", "patron_name"}),
}

# Code-level defaults — fields on when no admin setting is configured.
DEFAULT_FIELDS: dict[str, frozenset[str]] = {
    "spine":        frozenset({"call_number", "location", "cutter", "year"}),  # barcode off by default
    "pocket":       frozenset({"barcode", "title", "author", "call_number", "cutter", "year"}),
    "barcode-only": frozenset({"barcode", "human_readable"}),
    "full":         frozenset({"barcode", "card_number", "library_name", "subtitle", "patron_name", "expiry"}),
    "sticker":      frozenset({"barcode", "card_number"}),
}


def compatible_templates(kind: str) -> list[LabelTemplate]:
    """Return templates that make geometric sense for the given label kind."""
    if kind == "spine":
        return [t for t in TEMPLATES.values()
                if t.orientation == "rotated"
                or (t.label_width >= 2.0 and t.label_height <= 1.5)]
    if kind == "pocket":
        return [t for t in TEMPLATES.values()
                if t.orientation != "rotated" and t.label_height >= 0.9]
    if kind == "barcode-only":
        return [t for t in TEMPLATES.values() if t.orientation != "rotated"]
    if kind == "patron-full":
        return [t for t in TEMPLATES.values()
                if t.orientation != "rotated" and t.supports_full_card]
    if kind == "patron-sticker":
        return [t for t in TEMPLATES.values() if t.orientation != "rotated"]
    raise ValueError(f"unknown label kind {kind!r}")


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
    location: str | None = None  # e.g. "REFERENCE", "CHILDREN" — shown above call number on spine formats


@dataclass
class PatronCardRow:
    card_number: str
    full_name: str
    expires_at: date | None = None
    category_display: str | None = None  # e.g. "Adult", "Youth" — shown on patron-full when "category" field enabled


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


def _draw_barcode_vertical(
    c: canvas.Canvas,
    x: float,
    y: float,
    value: str,
    width: float,
    height: float,
    *,
    symbology: BarcodeSymbology,
) -> None:
    """Render a barcode rotated 90°, distributing modules along the y-axis.
    Use inside a saveState/restoreState block when the canvas is already rotated.
    The barcode runs from y upward for ``height`` points; ``width`` is the bar depth.
    No human-readable text (no space for it on a 0.5" label)."""
    pattern = _module_pattern(value, symbology)
    if not pattern:
        return
    module_height = height / len(pattern)
    for i, bit in enumerate(pattern):
        if bit == "1":
            c.rect(x, y + i * module_height, width, module_height, fill=1, stroke=0)


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
    fields: frozenset[str] | None = None,
    library_name: str | None = None,
) -> bytes:
    """Render item labels to PDF bytes.

    ``format`` defaults based on template geometry:
      - ``orientation="rotated"`` templates (e.g. avery-5167-spine) → 'spine-text'
      - aspect ratio ≥ 3.0 (wide & short, e.g. avery-5167 at 3.5) → 'barcode-only'
      - aspect ratio ≤ 0.67 (tall & narrow) → 'spine-text'
      - otherwise → 'pocket' (title + call number + cutter/year + barcode)
    Caller may override with an explicit ``format=`` argument.

    ``fields`` controls which elements appear on each label.  Pass a frozenset
    of field names from OPTIONAL_FIELDS[format]; omit to use the deployment's
    admin-configured defaults (falling back to DEFAULT_FIELDS).  Every field is
    optional — an empty frozenset produces a valid (possibly blank) label.

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
        if template.orientation == "rotated":
            format = "spine"
        else:
            aspect = template.label_width / template.label_height
            if aspect >= 3.0:    # wide and short (e.g. 5167 at 1.75/0.5=3.5) → barcode strip
                format = "barcode-only"
            elif aspect <= 0.67:  # tall and narrow → spine
                format = "spine"
            else:
                format = "pocket"

    # Backward-compat aliases for the old split kind names.
    if format in ("spine-text", "spine-barcode"):
        format = "spine"

    effective_fields = fields if fields is not None else DEFAULT_FIELDS.get(format, frozenset())

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(template.page_width * inch, template.page_height * inch))
    items_list = list(items)
    positions = _iter_label_positions(template, start_label)
    current_page = 0

    for row, (_, x, y, page_idx) in zip(items_list, positions):
        if page_idx != current_page:
            c.showPage()
            current_page = page_idx
        _draw_item_label(c, row, x, y, template, format, use_isbn_barcode, symbology, effective_fields, library_name)

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
    fields: frozenset[str],
    library_name: str | None = None,
) -> None:
    """Render one item label at the cell origin ``(x, y)``.

    For templates with ``orientation="rotated"`` (e.g. spine labels), the
    canvas is rotated 90° CCW so all inner-content drawing code can use the
    same coordinate system: a "tall" cell whose vertical extent is the
    physical long dimension. After rotation, ``lw``/``lh`` are swapped so
    that ``lw`` is the (narrow) horizontal extent and ``lh`` is the (long)
    vertical extent in the rotated drawing context.
    """
    rotated = (t.orientation == "rotated")
    lw = t.label_width * inch
    lh = t.label_height * inch

    if rotated:
        c.saveState()
        # Translate to bottom-right of the physical cell, then rotate 90° CCW.
        # That maps the rotated drawing origin onto the bottom-left of the
        # tall (portrait-oriented) cell, so all downstream code can draw
        # top→bottom in a familiar coordinate system.
        c.translate(x + lw, y)
        c.rotate(90)
        lw, lh = lh, lw  # swap: lw now physically-short, lh now physically-long
        x, y = 0.0, 0.0

    try:
        _draw_item_label_content(c, row, x, y, lw, lh, fmt, use_isbn, symbology, rotated, fields, library_name)
    finally:
        if rotated:
            c.restoreState()


def _draw_item_label_content(
    c: canvas.Canvas,
    row: ItemLabelRow,
    x: float,
    y: float,
    lw: float,   # in points, already swapped for rotated context
    lh: float,   # in points, already swapped for rotated context
    fmt: ItemFormat,
    use_isbn: bool,
    symbology: BarcodeSymbology,
    rotated: bool,
    fields: frozenset[str],
    library_name: str | None = None,
) -> None:
    pad = 4  # points
    inner_w = lw - 2 * pad

    font = "Helvetica-Bold"
    body_font = "Helvetica"

    cn_lines = wrap_call_number(row.call_number or "", max_chars=10)
    cutter_str = cutter(row.author_display)
    year = str(row.publication_year) if row.publication_year else ""

    if fmt == "barcode-only":
        title_size = 7
        title_reserved = 0
        if "title" in fields and row.title:
            title_reserved = title_size + 2
            top_y = y + lh - pad - title_size
            c.setFont(body_font, title_size)
            c.drawString(x + pad, top_y, _truncate(row.title, inner_w, body_font, title_size))
        if "barcode" in fields:
            hr = "human_readable" in fields
            bc_h = max(8.0, lh - 2 * pad - 2 - title_reserved)
            if use_isbn and row.isbn:
                _draw_barcode_ean13(
                    c, x + pad, y + pad, row.isbn, inner_w, bc_h,
                    fallback_symbology=symbology,
                    human_readable=hr,
                )
            else:
                _draw_barcode(
                    c, x + pad, y + pad, row.barcode, inner_w, bc_h,
                    symbology=symbology,
                    human_readable=hr,
                )
        return

    if fmt == "spine":
        # Fixed geometry so a missing call number doesn't shift the cutter/year
        # up: reserve space for optional branch/location, up to 4 call-number
        # lines, cutter, year, and (if barcode enabled) a barcode strip at bottom.
        cn_font_size = 9
        cutter_font_size = 10
        year_font_size = 9
        line_h_cn = cn_font_size + 1

        # When barcode is enabled, reserve a strip at the bottom of the cell.
        # In rotated context allocate 40% of the long dim so bars are tall
        # enough to scan. Non-rotated gets a fixed 14pt strip. When barcode is
        # off the strip collapses to zero and text reclaims the full height.
        draw_barcode = "barcode" in fields
        if draw_barcode:
            bc_strip = (lh - 2 * pad) * 0.40 if rotated else 14
        else:
            bc_strip = 0
        text_bottom = y + pad + bc_strip + (2 if bc_strip else 0)

        top = y + lh - pad

        # Optional branch line at the very top (small uppercase), above location.
        if "branch" in fields and row.branch_code:
            br_size = 7
            c.setFont(body_font, br_size)
            c.drawString(
                x + pad,
                top - br_size,
                _truncate(row.branch_code.upper(), inner_w, body_font, br_size),
            )
            top -= br_size + 2

        # Optional location line (small uppercase), below branch.
        if "location" in fields and row.location:
            loc_size = 7
            c.setFont(body_font, loc_size)
            c.drawString(
                x + pad,
                top - loc_size,
                _truncate(row.location.upper(), inner_w, body_font, loc_size),
            )
            top -= loc_size + 2

        # Call number block: up to 4 lines (optional; reserves fixed height so
        # cutter/year don't shift when call number is absent from the data).
        max_cn_lines = 4
        cursor = top - cn_font_size
        if "call_number" in fields:
            cn_slots = min(len(cn_lines), max_cn_lines)
            c.setFont(font, cn_font_size)
            for i in range(max_cn_lines):
                if i < cn_slots:
                    c.drawString(x + pad, cursor, _truncate(cn_lines[i], inner_w, font, cn_font_size))
                cursor -= line_h_cn
        else:
            cursor -= line_h_cn * max_cn_lines  # reclaim vertical space
        # Cutter (bold) + year (regular) on their own lines below the block.
        if "cutter" in fields and cutter_str:
            c.setFont(font, cutter_font_size)
            c.drawString(x + pad, max(cursor - 2, text_bottom), cutter_str)
        cursor -= cutter_font_size + 2
        if "year" in fields and year:
            c.setFont(body_font, year_font_size)
            c.drawString(x + pad, max(cursor - 2, text_bottom), year)

        # Optional barcode strip at the bottom (when "barcode" field is enabled).
        if draw_barcode:
            if rotated:
                # In a rotated drawing context, the local y-axis IS the
                # physical long dimension (1.75"). Run the barcode along it
                # so we have ~118pt of bar-distribution length instead of
                # cramming 100+ modules into the 0.5" short dim.
                _draw_barcode_vertical(
                    c,
                    x + pad,
                    y + pad,
                    row.barcode,
                    inner_w,      # bar depth: full inner width
                    bc_strip,     # bar length: reserved bottom portion (~40%)
                    symbology=symbology,
                )
            else:
                # Non-rotated spine: a horizontal strip at the bottom.
                _draw_barcode(
                    c,
                    x + pad,
                    y + pad,
                    row.barcode,
                    inner_w,
                    bc_strip,
                    symbology=symbology,
                    human_readable=False,
                )
        return

    # pocket format:
    #   header:   library name (optional, small, top)
    #   top row:  title (+ author) — optional; width constrained when branch shown
    #   middle:   call number + cutter + year (one line)
    #   corner:   branch (optional, top-right)
    #   bottom:   barcode (required)
    title_size = 8
    info_size = 9

    # Optional library-name header at the very top; pushes everything else down.
    header_h = 0
    if "library_name" in fields and library_name:
        header_size = 7
        header_h = header_size + 3
        c.setFont(body_font, header_size)
        c.drawString(
            x + pad,
            y + lh - pad - header_size,
            _truncate(library_name, inner_w, body_font, header_size),
        )

    top_y = y + lh - pad - header_h - title_size
    mid_y = top_y - title_size - 4

    # Reserve right corner for branch so title doesn't run into it.
    branch_reserved = (inner_w / 3 + 6) if ("branch" in fields and row.branch_code) else 0
    title_max_w = inner_w - branch_reserved

    # Title + optional author
    if "title" in fields and row.title:
        c.setFont(body_font, title_size)
        title_text = row.title
        if "author" in fields and row.author_display:
            title_text = f"{row.title} — {row.author_display}"
        c.drawString(x + pad, top_y, _truncate(title_text, title_max_w, body_font, title_size))

    # Call-number line: "PS3551 .E76 D8 · HER" style (middle)
    parts: list[str] = []
    if "call_number" in fields and cn_lines:
        parts.append(" ".join(cn_lines))
    if "cutter" in fields and cutter_str:
        parts.append(cutter_str)
    if "year" in fields and year and not ("call_number" in fields and cn_lines):
        parts.append(year)
    info_text = "  ·  ".join(parts) if parts else ""
    c.setFont(font, info_size)
    c.drawString(x + pad, mid_y, _truncate(info_text, inner_w, font, info_size))

    # Branch (small, top-right corner) — drawn at same baseline as title.
    if "branch" in fields and row.branch_code:
        br_size = 7
        c.setFont(body_font, br_size)
        c.drawRightString(
            x + lw - pad,
            top_y,
            _truncate(row.branch_code.upper(), inner_w / 3, body_font, br_size),
        )

    # Barcode at the bottom (optional)
    if "barcode" in fields:
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
    fields: frozenset[str] | None = None,
) -> bytes:
    """Render patron cards to PDF bytes.

    ``full`` mode (for 5871/5390 cardstock): library name header + patron name +
    card number + barcode + expiry.
    ``sticker`` mode (for 5160/5167 small labels): card number + barcode only,
    intended to be affixed to a pre-made card the library ordered separately.

    ``fields`` controls which optional elements appear.  Pass a frozenset of
    field names from OPTIONAL_FIELDS[format]; omit to use deployment defaults.

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
            f"such as 'avery-5871' or 'avery-22806'."
        )

    effective_fields = fields if fields is not None else DEFAULT_FIELDS.get(format, frozenset())

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
            _draw_patron_full(c, row, x, y, template, library_name, symbology, effective_fields)
        else:
            _draw_patron_sticker(c, row, x, y, template, symbology, effective_fields)

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
    fields: frozenset[str],
) -> None:
    lw = t.label_width * inch
    lh = t.label_height * inch
    pad = 8
    inner_w = lw - 2 * pad
    top = y + lh - pad

    # Library name header (optional)
    if "library_name" in fields:
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(x + lw / 2, top - 10,
                            _truncate(library_name, inner_w, "Helvetica-Bold", 10))
        top -= 10
    # "Library Card" subtitle (optional)
    if "subtitle" in fields:
        c.setFont("Helvetica", 8)
        c.drawCentredString(x + lw / 2, top - 12, "Library Card")
        top -= 12

    # Patron name (optional)
    if "patron_name" in fields:
        c.setFont("Helvetica", 11)
        c.drawCentredString(x + lw / 2, top - 22,
                            _truncate(row.full_name, inner_w, "Helvetica", 11))

    # Category (optional — small, right-aligned below patron name)
    if "category" in fields and row.category_display:
        c.setFont("Helvetica", 7)
        c.drawRightString(x + lw - pad, top - 22 - 9,
                          _truncate(row.category_display, inner_w / 2, "Helvetica", 7))

    # Barcode (optional); card_number controls whether text is printed below the bars.
    if "barcode" in fields:
        bc_h = 28
        bc_y = y + pad + 12
        bc_w = inner_w
        _draw_barcode(
            c, x + pad, bc_y, row.card_number, bc_w, bc_h,
            symbology=symbology, human_readable=("card_number" in fields),
        )

    # Expiry (bottom-right corner, optional)
    if "expiry" in fields and row.expires_at:
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
    fields: frozenset[str],
) -> None:
    lw = t.label_width * inch
    lh = t.label_height * inch
    pad = 2
    inner_w = lw - 2 * pad

    # Optional patron name at the top (small)
    name_reserved = 0
    if "patron_name" in fields and row.full_name:
        name_size = 7
        name_reserved = name_size + 2
        top_y = y + lh - pad - name_size
        c.setFont("Helvetica", name_size)
        c.drawString(x + pad, top_y, _truncate(row.full_name, inner_w, "Helvetica", name_size))

    # Barcode fills the remaining space (optional); card_number controls the text below.
    if "barcode" in fields:
        bc_h = lh - 2 * pad - 2 - name_reserved
        _draw_barcode(
            c, x + pad, y + pad, row.card_number, inner_w, bc_h,
            symbology=symbology, human_readable=("card_number" in fields),
        )
