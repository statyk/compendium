"""SVG backend for the label preview system.

Implements the same drawing interface used by the PDF (reportlab) backend so
``_draw_item_label_content`` can be called unchanged and produce SVG markup
instead of PDF byte stream instructions.

PDF and SVG have opposite y-axis conventions: PDF origin is bottom-left (y
increases upward); SVG origin is top-left (y increases downward). The
``to_svg()`` method wraps all content in a root ``<g transform="translate(0,H)
scale(1,-1)">`` group to map PDF coords into SVG space. Each text element also
carries a per-element ``scale(1,-1)`` counter-flip so glyph outlines render
right-side-up inside the flipped frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _css_family(font_name: str) -> str:
    base = font_name[:-5] if font_name.endswith("-Bold") else font_name
    return base


def _escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class _Frame:
    transforms: list[str] = field(default_factory=list)
    font_name: str = "Helvetica"
    font_size: float = 10.0
    buf: list[str] = field(default_factory=list)


class SVGLabelCanvas:
    """Drawing surface that accumulates SVG elements instead of PDF instructions.

    Satisfies the ``LabelCanvas`` protocol defined in ``services/labels.py``.
    Call ``to_svg()`` after all drawing operations to get the complete SVG string.
    """

    def __init__(self, width_pts: float, height_pts: float) -> None:
        self.w = width_pts
        self.h = height_pts
        self._stack: list[_Frame] = [_Frame()]

    @property
    def _frame(self) -> _Frame:
        return self._stack[-1]

    # ── Font state ──────────────────────────────────────────────────────

    def setFont(self, name: str, size: float) -> None:
        self._frame.font_name = name
        self._frame.font_size = size

    # ── Transform state stack ───────────────────────────────────────────

    def saveState(self) -> None:
        f = self._frame
        self._stack.append(_Frame(font_name=f.font_name, font_size=f.font_size))

    def restoreState(self) -> None:
        frame = self._stack.pop()
        if frame.transforms:
            transform_str = " ".join(frame.transforms)
            wrapped = [f'<g transform="{transform_str}">'] + frame.buf + ["</g>"]
        else:
            wrapped = frame.buf
        self._stack[-1].buf.extend(wrapped)

    def translate(self, dx: float, dy: float) -> None:
        self._frame.transforms.append(f"translate({dx:.4g},{dy:.4g})")

    def rotate(self, degrees: float) -> None:
        self._frame.transforms.append(f"rotate({degrees:.4g})")

    # ── Geometry ─────────────────────────────────────────────────────────

    def rect(self, x: float, y: float, w: float, h: float,
             fill: int = 0, stroke: int = 1) -> None:
        fill_attr = 'fill="black"' if fill else 'fill="none"'
        stroke_attr = 'stroke="black"' if stroke else 'stroke="none"'
        self._frame.buf.append(
            f'<rect x="{x:.4g}" y="{y:.4g}" width="{w:.4g}" height="{h:.4g}" '
            f'{fill_attr} {stroke_attr}/>'
        )

    # ── Text ─────────────────────────────────────────────────────────────

    def _font_attrs(self) -> str:
        name = self._frame.font_name
        size = self._frame.font_size
        attrs = f'font-family="{_css_family(name)}" font-size="{size:.4g}"'
        if name.endswith("-Bold"):
            attrs += ' font-weight="bold"'
        return attrs

    def _emit_text(self, x: float, y: float, text: str, anchor: str) -> None:
        self._frame.buf.append(
            f'<g transform="translate({x:.4g},{y:.4g}) scale(1,-1)">'
            f'<text x="0" y="0" text-anchor="{anchor}" {self._font_attrs()}>'
            f'{_escape(text)}</text></g>'
        )

    def drawString(self, x: float, y: float, text: str) -> None:
        self._emit_text(x, y, text, "start")

    def drawCentredString(self, x: float, y: float, text: str) -> None:
        self._emit_text(x, y, text, "middle")

    def drawRightString(self, x: float, y: float, text: str) -> None:
        self._emit_text(x, y, text, "end")

    # ── Serialization ─────────────────────────────────────────────────────

    def to_svg(self) -> str:
        """Return a complete SVG document string."""
        while len(self._stack) > 1:
            self.restoreState()

        inner = "\n".join(self._stack[0].buf)
        w, h = f"{self.w:.4g}", f"{self.h:.4g}"

        outline = (
            f'<rect x="0" y="0" width="{w}" height="{h}"'
            f' fill="white" stroke="#bbb" stroke-width="0.5"/>'
        )
        flip = f'<g transform="translate(0,{h}) scale(1,-1)">\n{inner}\n</g>'

        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {w} {h}" '
            f'preserveAspectRatio="xMidYMid meet">'
            f'{outline}{flip}</svg>'
        )
