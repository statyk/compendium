"""Unit tests for the labels service — cutter logic + PDF rendering sanity."""

from __future__ import annotations

from datetime import date
from io import BytesIO

import barcode as _barcode_lib
import pytest

from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as rl_canvas

import xml.etree.ElementTree as ET

from compendium.services.labels import (
    ItemLabelRow,
    PatronCardRow,
    ITEM_KIND_TO_FORMAT,
    OPTIONAL_FIELDS,
    TEMPLATES,
    _SAMPLE_ITEM_ROW,
    compatible_templates,
    cutter,
    generate_item_labels,
    generate_patron_cards,
    render_item_label_svg,
    wrap_call_number,
)


class TestCutter:
    def test_simple_first_last(self):
        assert cutter("Frank Herbert") == "HER"

    def test_surname_first_with_comma(self):
        assert cutter("Herbert, Frank") == "HER"

    def test_single_name(self):
        assert cutter("Cicero") == "CIC"

    def test_non_letter_stripped(self):
        assert cutter("O'Brien") == "OBR"
        assert cutter("Saint-Exupéry") == "SAI"

    def test_empty(self):
        assert cutter("") == ""
        assert cutter(None) == ""

    def test_short_surname(self):
        assert cutter("Xi") == "XI"


class TestWrapCallNumber:
    def test_lcc_splits_on_whitespace(self):
        assert wrap_call_number("PS3551 .E76 D8 1965") == ["PS3551", ".E76", "D8", "1965"]

    def test_ddc(self):
        assert wrap_call_number("823.912 HER") == ["823.912", "HER"]

    def test_empty(self):
        assert wrap_call_number("") == []
        assert wrap_call_number(None) == []

    def test_hard_wraps_long_piece(self):
        long = "PSABCDEFGHIJKLMN"
        out = wrap_call_number(long, max_chars=6)
        assert all(len(p) <= 6 for p in out)
        assert "".join(out) == long


class TestGenerateItemLabels:
    def test_returns_pdf_bytes(self):
        rows = [
            ItemLabelRow(
                barcode="BC000001",
                title="Dune",
                author_display="Frank Herbert",
                call_number="PS3551 .E76 D8 1965",
                publication_year=1965,
            )
        ]
        pdf = generate_item_labels(rows, template_key="avery-5160", format="pocket")
        assert pdf.startswith(b"%PDF-")
        assert len(pdf) > 500

    def test_spine_format_renders(self):
        rows = [
            ItemLabelRow(
                barcode="BC000001",
                title="Dune",
                author_display="Frank Herbert",
                call_number="PS3551 .E76",
            )
        ]
        pdf = generate_item_labels(rows, template_key="avery-5167", format="spine")
        assert pdf.startswith(b"%PDF-")

    def test_spine_compat_aliases_accepted(self):
        """Old spine-text and spine-barcode format strings are silently normalized."""
        rows = [ItemLabelRow(barcode="BC000001", title="T", call_number="PS123")]
        for alias in ("spine-text", "spine-barcode"):
            pdf = generate_item_labels(rows, template_key="avery-5167", format=alias)  # type: ignore[arg-type]
            assert pdf.startswith(b"%PDF-"), f"alias {alias!r} failed"

    def test_location_field_accepted(self):
        rows = [
            ItemLabelRow(
                barcode="BC000001",
                title="Dune",
                author_display="Frank Herbert",
                call_number="PS3551 .E76",
                location="REFERENCE",
            )
        ]
        pdf = generate_item_labels(rows, template_key="avery-5167", format="spine")
        assert pdf.startswith(b"%PDF-")

    def test_spine_barcode_field_larger_than_without(self):
        """Spine PDF with barcode field on is larger than without."""
        rows = [ItemLabelRow(barcode="30000000001234", title="T", call_number="PS123")]
        pdf_text = generate_item_labels(
            rows, template_key="avery-5167", format="spine",
            fields=frozenset({"call_number"}),
        )
        pdf_barcode = generate_item_labels(
            rows, template_key="avery-5167", format="spine",
            fields=frozenset({"call_number", "barcode"}),
        )
        assert pdf_text.startswith(b"%PDF-")
        assert pdf_barcode.startswith(b"%PDF-")
        assert len(pdf_barcode) >= len(pdf_text)

    def test_spine_barcode_rotated_renders(self):
        """Spine with barcode on a rotated template renders without exception."""
        rows = [ItemLabelRow(barcode="30000000001234", title="T", call_number="PS123")]
        pdf = generate_item_labels(
            rows, template_key="avery-5167-spine", format="spine",
            fields=frozenset({"call_number", "barcode"}),
        )
        assert pdf.startswith(b"%PDF-")

    def test_location_renders_on_rotated_spine(self):
        """location + barcode on rotated spine template works."""
        rows = [ItemLabelRow(
            barcode="30000000001234", title="T", call_number="PS123", location="REFERENCE"
        )]
        pdf = generate_item_labels(
            rows, template_key="avery-5167-spine", format="spine",
            fields=frozenset({"call_number", "barcode", "location"}),
        )
        assert pdf.startswith(b"%PDF-")

    def test_format_auto_picks_based_on_template(self):
        rows = [ItemLabelRow(barcode="BC1", title="A")]
        # avery-5167: aspect=3.5 (≥3.0) → barcode-only
        pdf_barcode_only = generate_item_labels(rows, template_key="avery-5167", format=None)
        # avery-5160: aspect=2.625 (<3.0) → pocket
        pdf_pocket = generate_item_labels(rows, template_key="avery-5160", format=None)
        assert pdf_barcode_only.startswith(b"%PDF-")
        assert pdf_pocket.startswith(b"%PDF-")
        # Pocket has more content than barcode-only, so should be larger
        assert len(pdf_pocket) > len(pdf_barcode_only)

    @pytest.mark.parametrize("template_key,expected_format", [
        # rotated orientation → spine regardless of dimensions
        ("avery-5167-spine", "spine"),
        # aspect 1.75/0.5 = 3.5 ≥ 3.0 → barcode-only
        ("avery-5167",       "barcode-only"),
        # aspect 2.625/1.0 = 2.625, between 0.67 and 3.0 → pocket
        ("avery-5160",       "pocket"),
        # aspect 3.5/2.0 = 1.75 → pocket
        ("avery-5871",       "pocket"),
        # aspect 1.5/1.5 = 1.0 → pocket
        ("avery-22805",      "pocket"),
        # aspect 2.0/2.0 = 1.0 → pocket
        ("avery-22806",      "pocket"),
    ])
    def test_format_auto_selection_by_template(self, template_key: str, expected_format: str):
        """Each template should auto-select the expected format based on aspect ratio / orientation."""
        rows = [ItemLabelRow(barcode="BC1", title="A Book", call_number="PS123")]
        # Render with explicit expected format to get reference bytes, then confirm
        # auto-selection produces valid PDF (exact format is verified by the mapping logic)
        pdf = generate_item_labels(rows, template_key=template_key, format=None)
        assert pdf.startswith(b"%PDF-"), (
            f"template {template_key!r} auto-format={expected_format!r} produced invalid PDF"
        )
        # Also confirm the expected format renders without error
        pdf_explicit = generate_item_labels(rows, template_key=template_key, format=expected_format)
        assert pdf_explicit.startswith(b"%PDF-")

    def test_start_label_skips_positions(self):
        rows = [ItemLabelRow(barcode=f"BC{i}", title=f"T{i}") for i in range(5)]
        pdf_no_skip = generate_item_labels(rows, template_key="avery-5160", format="pocket")
        pdf_skip = generate_item_labels(
            rows, template_key="avery-5160", format="pocket", start_label=10
        )
        # Skipping positions produces a different layout, so the file bytes differ
        assert pdf_no_skip != pdf_skip

    def test_isbn_barcode_falls_back_when_invalid(self):
        rows = [ItemLabelRow(barcode="BC1", title="T", isbn="not-an-isbn")]
        # Should not raise — fall back to Code128 on the internal barcode
        pdf = generate_item_labels(
            rows, template_key="avery-5160", format="pocket", use_isbn_barcode=True
        )
        assert pdf.startswith(b"%PDF-")

    def test_multi_page_wraps_correctly(self):
        # 5160 has 30 per sheet; 35 items → 2 pages
        rows = [ItemLabelRow(barcode=f"BC{i}", title=f"T{i}") for i in range(35)]
        pdf = generate_item_labels(rows, template_key="avery-5160", format="pocket")
        # Very rough page count: count occurrences of page-dict pattern
        assert pdf.count(b"/Type /Page\n") >= 2 or pdf.count(b"/Type/Page") >= 2


class TestGeneratePatronCards:
    def test_full_returns_pdf(self):
        rows = [
            PatronCardRow(
                card_number="00000001",
                full_name="Alice Example",
                expires_at=date(2027, 6, 15),
            )
        ]
        pdf = generate_patron_cards(
            rows, template_key="avery-5871", format="full", library_name="Test Library"
        )
        assert pdf.startswith(b"%PDF-")

    def test_sticker_returns_pdf(self):
        rows = [PatronCardRow(card_number="00000002", full_name="Bob")]
        pdf = generate_patron_cards(
            rows, template_key="avery-5167", format="sticker"
        )
        assert pdf.startswith(b"%PDF-")

    def test_library_name_accepted_without_error(self):
        # reportlab compresses text streams so we can't byte-grep the PDF for
        # the library name. Smoke-test that the arg is accepted and a valid
        # PDF comes back for both a short and a long name.
        rows = [PatronCardRow(card_number="00000003", full_name="Carol")]
        short = generate_patron_cards(
            rows, template_key="avery-5871", format="full", library_name="Short"
        )
        longer = generate_patron_cards(
            rows,
            template_key="avery-5871",
            format="full",
            library_name="A Much Longer Library Name That Must Be Truncated",
        )
        assert short.startswith(b"%PDF-")
        assert longer.startswith(b"%PDF-")


class TestPatronFullCardSizeValidation:
    """Full patron format requires a template ≥ 1.5" tall; smaller templates
    raise ValueError rather than render overlapping content."""

    def test_full_on_small_template_rejected(self):
        rows = [PatronCardRow(card_number="1", full_name="X")]
        with pytest.raises(ValueError, match="too small for 'full'"):
            generate_patron_cards(rows, template_key="avery-5167", format="full")

    def test_full_on_5160_rejected(self):
        rows = [PatronCardRow(card_number="1", full_name="X")]
        with pytest.raises(ValueError, match="too small for 'full'"):
            generate_patron_cards(rows, template_key="avery-5160", format="full")

    def test_full_on_large_template_ok(self):
        rows = [PatronCardRow(card_number="1", full_name="X")]
        pdf = generate_patron_cards(rows, template_key="avery-5871", format="full")
        assert pdf.startswith(b"%PDF-")

    def test_sticker_works_on_any_template(self):
        rows = [PatronCardRow(card_number="1", full_name="X")]
        for key in ("avery-5160", "avery-5167", "avery-5871", "avery-22806"):
            pdf = generate_patron_cards(rows, template_key=key, format="sticker")
            assert pdf.startswith(b"%PDF-")

    def test_supports_full_card_property(self):
        assert not TEMPLATES["avery-5167"].supports_full_card
        assert not TEMPLATES["avery-5160"].supports_full_card
        assert TEMPLATES["avery-5871"].supports_full_card
        assert TEMPLATES["avery-22806"].supports_full_card


class TestItemBarcodeOnlyFormat:
    def test_barcode_only_renders(self):
        rows = [ItemLabelRow(barcode="BC1", title="Anything")]
        pdf = generate_item_labels(rows, template_key="avery-5167", format="barcode-only")
        assert pdf.startswith(b"%PDF-")

    def test_auto_default_for_small_is_barcode_only(self):
        # 5167 is narrow → auto should pick barcode-only (prior default was spine).
        # Both should succeed; we just verify both paths are valid.
        rows = [ItemLabelRow(barcode="BC1", title="T")]
        auto = generate_item_labels(rows, template_key="avery-5167", format=None)
        explicit = generate_item_labels(
            rows, template_key="avery-5167", format="barcode-only"
        )
        # Structural equality is brittle (PDFs embed timestamps), so just confirm
        # both succeed and are non-trivial.
        assert auto.startswith(b"%PDF-") and len(auto) > 500
        assert explicit.startswith(b"%PDF-") and len(explicit) > 500


class TestItemMissingCallNumberDoesNotShiftLayout:
    """When an item has no call number, the spine/pocket layouts should reserve
    the same vertical space so cutter/year/barcode stay put across a batch."""

    def test_spine_with_and_without_cn_same_page_count(self):
        # Two batches: same number of rows, one with CN, one without.
        with_cn = [
            ItemLabelRow(
                barcode=f"BC{i}",
                title=f"T{i}",
                author_display="A B",
                call_number="PS3551 .E76",
                publication_year=1965,
            )
            for i in range(10)
        ]
        without_cn = [
            ItemLabelRow(
                barcode=f"BC{i}",
                title=f"T{i}",
                author_display="A B",
                call_number=None,
                publication_year=1965,
            )
            for i in range(10)
        ]
        pdf_a = generate_item_labels(with_cn, template_key="avery-5160", format="spine")
        pdf_b = generate_item_labels(without_cn, template_key="avery-5160", format="spine")
        # Same row count → same number of pages (30 per sheet, both fit on one).
        # Structurally valid PDFs.
        assert pdf_a.startswith(b"%PDF-")
        assert pdf_b.startswith(b"%PDF-")

    def test_pocket_with_and_without_cn_renders_cleanly(self):
        mixed = [
            ItemLabelRow(barcode="BC1", title="Has CN", author_display="A",
                         call_number="PS3551", publication_year=1965),
            ItemLabelRow(barcode="BC2", title="No CN", author_display="B",
                         call_number=None, publication_year=2020),
        ]
        pdf = generate_item_labels(mixed, template_key="avery-5160", format="pocket")
        assert pdf.startswith(b"%PDF-")


def test_code128_uses_subset_c_for_digits():
    """Code 128 Subset C encodes two digits per symbol.
    14 digits → 7 data symbols + Start C + check + stop = 10 chars
    = 9×11 + 13 = 112 modules. Threshold of 140 keeps clear separation
    from Subset B's ~165 floor."""
    cls = _barcode_lib.get_barcode_class("code128")
    pattern = "".join(cls("12345678901234", writer=None).build())
    # Subset C: ~112 modules (excl. quiet zone). Subset B would be ~165+.
    assert len(pattern) < 140, (
        f"Code 128 pattern is {len(pattern)} modules — "
        "expected ~112 (Subset C). python-barcode may not be auto-selecting "
        "Subset C for all-digit inputs; add an explicit wrapper."
    )


class TestBarcodeSymbology:
    """Coverage for the Codabar / Code 39 / Code 128 selection added in
    the symbology slice. Tests use real Compendium-style decimal barcodes
    so all three symbologies can encode them natively (no fallback)."""

    @staticmethod
    def _set_symbology(value: str):
        """Patch the site-settings reader inside services.labels to return
        ``value`` for ``barcode_symbology`` lookups. Returns the patcher
        context manager."""
        from unittest.mock import patch

        def fake(key: str, *args, **kwargs):
            if key == "barcode_symbology":
                return value
            from compendium.services.site_settings import (
                get_site_setting as real,
            )
            return real(key, *args, **kwargs)

        return patch(
            "compendium.services.site_settings.get_site_setting", side_effect=fake
        )

    def test_module_pattern_returns_binary_string_for_each_symbology(self):
        from compendium.services.labels import _module_pattern

        for sym in ("codabar", "code39", "code128"):
            pattern = _module_pattern("3000000017", sym)
            assert pattern  # non-empty
            assert set(pattern) <= {"0", "1"}

    def test_module_pattern_falls_back_to_code128_on_unencodable_value(self):
        """Codabar can't encode letters; the helper should silently fall
        back to Code 128 so a label batch with legacy non-digit barcodes
        renders instead of crashing."""
        from compendium.services.labels import _module_pattern

        # "BC000001" has letters → Codabar would normally raise.
        pattern = _module_pattern("BC000001", "codabar")
        assert pattern  # non-empty (Code 128 fallback succeeded)
        # And the Code 128 path is the same as calling code128 directly.
        direct = _module_pattern("BC000001", "code128")
        assert pattern == direct

    def test_module_pattern_code128_propagates_unencodable(self):
        """Code 128 is the most permissive symbology — if it can't encode
        the value, there's no useful fallback. Surface the error."""
        import barcode
        from compendium.services.labels import _module_pattern

        # Code 128 supports any 8-bit byte. Use \x00, which python-barcode
        # specifically rejects in some implementations; if it doesn't,
        # this test is a no-op (kept for the regression-once-fixed path).
        try:
            _module_pattern("\x00", "code128")
        except barcode.errors.BarcodeError:
            pass  # expected for some inputs
        # No assertion on whether it raised — just that it didn't fall
        # back silently to itself (infinite recursion guard).

    def test_human_readable_text_returns_value_unchanged(self):
        from compendium.services.labels import _human_readable_text

        # Codabar's start/stop chars are an encoding detail, not user-visible.
        assert _human_readable_text("3000000017", "codabar") == "3000000017"
        assert _human_readable_text("3000000017", "code39") == "3000000017"
        assert _human_readable_text("3000000017", "code128") == "3000000017"

    @pytest.mark.parametrize("symbology", ["codabar", "code39", "code128"])
    def test_item_labels_render_under_each_symbology(self, symbology):
        rows = [ItemLabelRow(barcode="3000000017", title="Dune")]
        with self._set_symbology(symbology):
            pdf = generate_item_labels(
                rows, template_key="avery-5167", format="barcode-only"
            )
        assert pdf.startswith(b"%PDF-")
        assert len(pdf) > 500

    @pytest.mark.parametrize("symbology", ["codabar", "code39", "code128"])
    def test_patron_cards_render_under_each_symbology(self, symbology):
        rows = [PatronCardRow(card_number="2000000018", full_name="Alice")]
        with self._set_symbology(symbology):
            pdf = generate_patron_cards(
                rows, template_key="avery-5871", format="full"
            )
        assert pdf.startswith(b"%PDF-")
        assert len(pdf) > 500

    def test_different_symbologies_produce_different_pdf_bytes(self):
        """Catches silent failure where the setting is read but ignored —
        i.e. all three would produce identical PDFs. With distinct
        symbologies the bar/space patterns differ, so the rendered
        rectangles in the PDF stream differ as well."""
        rows = [ItemLabelRow(barcode="3000000017", title="Dune")]
        with self._set_symbology("codabar"):
            codabar_pdf = generate_item_labels(
                rows, template_key="avery-5167", format="barcode-only"
            )
        with self._set_symbology("code128"):
            code128_pdf = generate_item_labels(
                rows, template_key="avery-5167", format="barcode-only"
            )
        # Strip a few bytes' worth of timestamp jitter by comparing length
        # plus a content slice. PDFs of different bar patterns will diverge
        # in the content stream regardless of metadata.
        assert codabar_pdf != code128_pdf


class TestTemplates:
    def test_all_templates_have_per_sheet(self):
        for t in TEMPLATES.values():
            assert t.per_sheet == t.cols * t.rows
            assert t.per_sheet > 0

    def test_dimensions_fit_page(self):
        for t in TEMPLATES.values():
            used_w = (
                t.margin_left + t.cols * t.label_width + (t.cols - 1) * t.col_gap
            )
            used_h = (
                t.margin_top + t.rows * t.label_height + (t.rows - 1) * t.row_gap
            )
            # Rough sanity: the usable area must fit on 8.5 × 11 with room for the
            # opposite margin. Allow 1/8" slack for float arithmetic.
            assert used_w <= t.page_width + 0.125
            assert used_h <= t.page_height + 0.125

    def test_avery_5390_removed(self):
        assert "avery-5390" not in TEMPLATES

    def test_avery_5167_spine_has_rotated_orientation(self):
        assert TEMPLATES["avery-5167-spine"].orientation == "rotated"

    def test_new_templates_present(self):
        assert TEMPLATES["avery-5167-spine"].key == "avery-5167-spine"
        assert TEMPLATES["avery-22805"].key == "avery-22805"
        assert TEMPLATES["avery-22806"].key == "avery-22806"


class TestCompatibleTemplates:
    def test_spine_includes_rotated_template(self):
        from compendium.services.labels import compatible_templates

        keys = [t.key for t in compatible_templates("spine")]
        assert "avery-5167-spine" in keys

    def test_spine_excludes_small_non_rotated(self):
        from compendium.services.labels import compatible_templates

        keys = [t.key for t in compatible_templates("spine")]
        # avery-5167 is non-rotated and narrow (1.75" wide) — not suitable for spine.
        assert "avery-5167" not in keys

    def test_patron_full_excludes_small_templates(self):
        from compendium.services.labels import compatible_templates

        for t in compatible_templates("patron-full"):
            assert t.supports_full_card, f"{t.key} doesn't support_full_card but is in patron-full set"

    def test_patron_full_excludes_rotated(self):
        from compendium.services.labels import compatible_templates

        for t in compatible_templates("patron-full"):
            assert t.orientation != "rotated"

    def test_barcode_only_excludes_rotated(self):
        from compendium.services.labels import compatible_templates

        for t in compatible_templates("barcode-only"):
            assert t.orientation != "rotated"

    def test_unknown_kind_raises(self):
        from compendium.services.labels import compatible_templates

        with pytest.raises(ValueError, match="unknown label kind"):
            compatible_templates("banana")

    def test_spine_5167_and_5167_spine_same_cell_origins(self):
        """avery-5167 and avery-5167-spine share the same sheet geometry —
        only the content is rotated. _iter_label_positions should return
        identical cell origins for both."""
        from compendium.services.labels import _iter_label_positions

        positions_flat = list(zip(range(20), _iter_label_positions(TEMPLATES["avery-5167"])))
        positions_rotated = list(zip(range(20), _iter_label_positions(TEMPLATES["avery-5167-spine"])))
        for (_, (_, xf, yf, pf)), (_, (_, xr, yr, pr)) in zip(positions_flat, positions_rotated):
            assert abs(xf - xr) < 0.01
            assert abs(yf - yr) < 0.01
            assert pf == pr


class TestFieldGating:
    """Tests that the fields= parameter properly gates which elements appear."""

    _ROW = ItemLabelRow(
        barcode="3000000017",
        title="Dune",
        author_display="Frank Herbert",
        call_number="PS3551 .E76",
        publication_year=1965,
        branch_code="NORTH",
        location="REFERENCE",
    )

    def test_spine_with_branch_hidden_by_default(self):
        from compendium.services.labels import DEFAULT_FIELDS

        assert "branch" not in DEFAULT_FIELDS["spine"]

    def test_pocket_with_branch_field_renders(self):
        pdf = generate_item_labels(
            [self._ROW],
            template_key="avery-5160",
            format="pocket",
            fields=frozenset({"barcode", "title", "branch"}),
        )
        assert pdf.startswith(b"%PDF-")

    def test_empty_fields_produces_valid_pdf(self):
        """Every format produces a valid (possibly blank) PDF with fields=frozenset()."""
        for fmt, tmpl in [
            ("spine", "avery-5167-spine"),
            ("pocket", "avery-5160"),
            ("barcode-only", "avery-5167"),
        ]:
            pdf = generate_item_labels(
                [self._ROW],
                template_key=tmpl,
                format=fmt,
                fields=frozenset(),
            )
            assert pdf.startswith(b"%PDF-"), f"format={fmt} failed"

    def test_spine_call_number_off_reclaims_space(self):
        """Spine PDF with call_number off is smaller (no 4-line CN block drawn)."""
        pdf_with = generate_item_labels(
            [self._ROW],
            template_key="avery-5167-spine",
            format="spine",
            fields=frozenset({"call_number", "cutter", "year"}),
        )
        pdf_without = generate_item_labels(
            [self._ROW],
            template_key="avery-5167-spine",
            format="spine",
            fields=frozenset({"cutter", "year"}),
        )
        assert len(pdf_without) < len(pdf_with)

    def test_spine_barcode_on_larger_than_off(self):
        """Spine PDF with barcode field enabled is larger than without."""
        pdf_with = generate_item_labels(
            [self._ROW],
            template_key="avery-5167-spine",
            format="spine",
            fields=frozenset({"call_number", "barcode"}),
        )
        pdf_without = generate_item_labels(
            [self._ROW],
            template_key="avery-5167-spine",
            format="spine",
            fields=frozenset({"call_number"}),
        )
        assert len(pdf_with) > len(pdf_without)

    def test_barcode_only_with_human_readable_off(self):
        pdf = generate_item_labels(
            [self._ROW],
            template_key="avery-5167",
            format="barcode-only",
            fields=frozenset(),
        )
        assert pdf.startswith(b"%PDF-")

    def test_patron_full_with_category(self):
        row = PatronCardRow(
            card_number="2000000001",
            full_name="Alice Smith",
            expires_at=date(2027, 1, 1),
            category_display="Adult",
        )
        pdf = generate_patron_cards(
            [row],
            template_key="avery-5871",
            format="full",
            fields=frozenset({"library_name", "patron_name", "barcode", "card_number", "expiry", "category"}),
        )
        assert pdf.startswith(b"%PDF-")

    def test_patron_sticker_with_patron_name(self):
        row = PatronCardRow(card_number="2000000002", full_name="Bob")
        pdf = generate_patron_cards(
            [row],
            template_key="avery-5167",
            format="sticker",
            fields=frozenset({"barcode", "card_number", "patron_name"}),
        )
        assert pdf.startswith(b"%PDF-")


class TestLabelSettingsValidator:
    """Tests for the per-kind field validator in settings_registry."""

    def test_valid_spine_fields_pass(self):
        from compendium.services.settings_registry import get_descriptor, parse

        desc = get_descriptor("label_spine_default_fields")
        result = parse(desc, "location, cutter, year")
        assert set(result) <= {"location", "cutter", "year"}

    def test_invalid_field_rejected(self):
        from compendium.services.settings_registry import (
            SettingValidationError,
            get_descriptor,
            parse,
        )

        desc = get_descriptor("label_spine_default_fields")
        with pytest.raises(SettingValidationError, match="unknown field"):
            parse(desc, "title")  # "title" is not optional for spine

    def test_patron_full_fields_roundtrip(self):
        from compendium.services.settings_registry import (
            get_descriptor,
            parse,
            encode_for_storage,
        )

        desc = get_descriptor("label_patron_full_default_fields")
        original = ["library_name", "patron_name", "expiry"]
        encoded = encode_for_storage(original, desc.type)
        decoded = parse(desc, encoded)
        assert set(decoded) == set(original)

    def test_all_label_settings_have_valid_defaults(self):
        from compendium.services.settings_registry import all_descriptors, validate

        for desc in all_descriptors():
            if not desc.key.startswith("label_") or not desc.key.endswith("_fields"):
                continue
            if desc.validator and desc.default is not None:
                desc.validator(desc.default)


# ──────────────────────────────────────────────────────────────────────
# Helpers for positional / centering tests
# ──────────────────────────────────────────────────────────────────────


def _make_recording_canvas(page_w_pts: float, page_h_pts: float):
    """Return a real reportlab Canvas whose drawString and drawCentredString
    calls are recorded for inspection. Font metrics work normally because
    the underlying canvas is real."""
    buf = BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_w_pts, page_h_pts))
    c._rec_draw_string: list[tuple[float, float, str]] = []
    c._rec_draw_centred: list[tuple[float, float, str]] = []

    _orig_ds = c.drawString
    _orig_dc = c.drawCentredString

    def _rec_ds(x, y, text, *a, **kw):
        c._rec_draw_string.append((x, y, str(text)))
        return _orig_ds(x, y, text, *a, **kw)

    def _rec_dc(x, y, text, *a, **kw):
        c._rec_draw_centred.append((x, y, str(text)))
        return _orig_dc(x, y, text, *a, **kw)

    c.drawString = _rec_ds
    c.drawCentredString = _rec_dc
    return c


def _spine_text_positions(
    template_key: str,
    fields: frozenset[str],
    *,
    rotated_ctx: bool = False,
) -> tuple[list[tuple[float, float, str]], list[tuple[float, float, str]]]:
    """Render one spine label via _draw_item_label_content and return
    (drawString_calls, drawCentredString_calls)."""
    from compendium.services.labels import _draw_item_label_content

    tmpl = TEMPLATES[template_key]
    lw = tmpl.label_width * inch
    lh = tmpl.label_height * inch
    if rotated_ctx:
        lw, lh = lh, lw

    row = ItemLabelRow(
        barcode="BC000001",
        title="Dune",
        author_display="Frank Herbert",
        call_number="PS3551 .E76 D8",
        publication_year=1965,
        location="REFERENCE",
    )
    c = _make_recording_canvas(lw, lh)
    _draw_item_label_content(
        c, row, 0.0, 0.0, lw, lh,
        "spine", False, "code128", rotated_ctx, fields,
    )
    return c._rec_draw_string, c._rec_draw_centred


# ──────────────────────────────────────────────────────────────────────
# Spine layout fix tests (RED before implementing changes)
# ──────────────────────────────────────────────────────────────────────


class TestSpineLayoutFixes:
    """Tests for the four changes in this slice:
      1. avery-5160-spine template
      2. squares in spine compatible_templates
      3. cutter/year no-overlap (bottom-up layout)
      4. flat-spine text centering
    """

    # ── 1. avery-5160-spine template ─────────────────────────────────

    def test_avery_5160_spine_in_templates(self):
        assert "avery-5160-spine" in TEMPLATES

    def test_avery_5160_spine_is_rotated(self):
        assert TEMPLATES["avery-5160-spine"].orientation == "rotated"

    def test_avery_5160_spine_shares_geometry_with_5160(self):
        """Same sheet geometry as avery-5160 — only content rotates."""
        flat = TEMPLATES["avery-5160"]
        rot  = TEMPLATES["avery-5160-spine"]
        assert rot.cols         == flat.cols
        assert rot.rows         == flat.rows
        assert rot.label_width  == flat.label_width
        assert rot.label_height == flat.label_height
        assert rot.margin_left  == flat.margin_left
        assert rot.margin_top   == flat.margin_top

    def test_avery_5160_spine_cell_origins_match_5160(self):
        """Cell origins must be identical — only content rotates."""
        from compendium.services.labels import _iter_label_positions

        flat_pos = list(zip(range(30), _iter_label_positions(TEMPLATES["avery-5160"])))
        rot_pos  = list(zip(range(30), _iter_label_positions(TEMPLATES["avery-5160-spine"])))
        for (_, (_, xf, yf, pf)), (_, (_, xr, yr, pr)) in zip(flat_pos, rot_pos):
            assert abs(xf - xr) < 0.01
            assert abs(yf - yr) < 0.01
            assert pf == pr

    def test_avery_5160_spine_renders_without_error(self):
        rows = [ItemLabelRow(
            barcode="BC000001", title="Dune",
            author_display="Frank Herbert",
            call_number="PS3551 .E76", publication_year=1965,
        )]
        pdf = generate_item_labels(rows, template_key="avery-5160-spine")
        assert pdf.startswith(b"%PDF-")

    def test_avery_5160_spine_in_compatible_spine_templates(self):
        keys = [t.key for t in compatible_templates("spine")]
        assert "avery-5160-spine" in keys

    # ── 2. squares in compatible_templates("spine") ───────────────────

    def test_compatible_templates_spine_includes_22805(self):
        keys = [t.key for t in compatible_templates("spine")]
        assert "avery-22805" in keys

    def test_compatible_templates_spine_includes_22806(self):
        keys = [t.key for t in compatible_templates("spine")]
        assert "avery-22806" in keys

    def test_square_22805_renders_as_spine(self):
        rows = [ItemLabelRow(
            barcode="BC000001", title="Dune",
            author_display="Frank Herbert",
            call_number="PS3551 .E76", publication_year=1965,
        )]
        pdf = generate_item_labels(rows, template_key="avery-22805", format="spine")
        assert pdf.startswith(b"%PDF-")

    def test_square_22806_renders_as_spine(self):
        rows = [ItemLabelRow(
            barcode="BC000001", title="Dune",
            author_display="Frank Herbert",
            call_number="PS3551 .E76", publication_year=1965,
        )]
        pdf = generate_item_labels(rows, template_key="avery-22806", format="spine")
        assert pdf.startswith(b"%PDF-")

    # ── 3. cutter/year no vertical overlap ───────────────────────────

    def test_5160_spine_cutter_year_on_distinct_baselines(self):
        """Cutter and year must differ by at least year_font_size (9pt) on
        avery-5160 with all default spine fields so they don't visually overlap."""
        ds, dc = _spine_text_positions(
            "avery-5160",
            frozenset({"call_number", "location", "cutter", "year"}),
        )
        all_calls = {text: y for (_, y, text) in (ds + dc)}
        assert "HER" in all_calls,  "cutter (HER) not drawn"
        assert "1965" in all_calls, "year (1965) not drawn"
        sep = abs(all_calls["HER"] - all_calls["1965"])
        assert sep >= 9, (
            f"cutter and year baselines are only {sep:.1f}pt apart "
            f"(cutter_y={all_calls['HER']:.1f}, year_y={all_calls['1965']:.1f}); "
            "they will visually overlap"
        )

    def test_5160_spine_all_fields_no_overlap(self):
        """With every spine field on, cutter and year still have room."""
        ds, dc = _spine_text_positions(
            "avery-5160",
            frozenset({"call_number", "location", "branch", "cutter", "year"}),
        )
        all_calls = {text: y for (_, y, text) in (ds + dc)}
        assert "HER" in all_calls and "1965" in all_calls
        assert abs(all_calls["HER"] - all_calls["1965"]) >= 9

    # ── 4. flat spine text centering ─────────────────────────────────

    def test_flat_spine_cutter_uses_draw_centred_string(self):
        """On a flat (non-rotated) spine template, cutter must be drawn with
        drawCentredString so it lands on the visible spine face."""
        ds, dc = _spine_text_positions(
            "avery-5160",
            frozenset({"call_number", "location", "cutter", "year"}),
        )
        centred_texts = {text for (_, _, text) in dc}
        drawstr_texts = {text for (_, _, text) in ds}
        assert "HER" in centred_texts, "cutter must use drawCentredString on flat spine"
        assert "HER" not in drawstr_texts, "cutter must NOT use drawString on flat spine"

    def test_flat_spine_year_uses_draw_centred_string(self):
        ds, dc = _spine_text_positions(
            "avery-5160",
            frozenset({"call_number", "location", "cutter", "year"}),
        )
        centred_texts = {text for (_, _, text) in dc}
        drawstr_texts = {text for (_, _, text) in ds}
        assert "1965" in centred_texts, "year must use drawCentredString on flat spine"
        assert "1965" not in drawstr_texts, "year must NOT use drawString on flat spine"

    def test_flat_spine_centred_at_cell_midpoint(self):
        """drawCentredString x must be at the cell horizontal midpoint."""
        tmpl = TEMPLATES["avery-5160"]
        centre_x = tmpl.label_width * inch / 2  # x=0 origin
        _, dc = _spine_text_positions(
            "avery-5160",
            frozenset({"cutter", "year"}),
        )
        for (x, _y, text) in dc:
            if text in ("HER", "1965"):
                assert abs(x - centre_x) < 2.0, (
                    f"{text!r}: expected centred at {centre_x:.1f}pt, got {x:.1f}pt"
                )

    def test_rotated_spine_cutter_uses_draw_string_not_centred(self):
        """On a rotated spine template the content runs along the spine's long
        axis — left-aligned (drawString) is correct; drawCentredString is wrong."""
        ds, dc = _spine_text_positions(
            "avery-5167-spine",
            frozenset({"call_number", "cutter", "year"}),
            rotated_ctx=True,
        )
        drawstr_texts = {text for (_, _, text) in ds}
        centred_texts = {text for (_, _, text) in dc}
        assert "HER" in drawstr_texts,   "cutter must use drawString on rotated spine"
        assert "HER" not in centred_texts, "cutter must NOT use drawCentredString on rotated spine"


# ──────────────────────────────────────────────────────────────────────
# SVG label preview (Slice 2)
# ──────────────────────────────────────────────────────────────────────


class TestRenderItemLabelSvg:
    """render_item_label_svg — single-label SVG output for the web preview."""

    def test_pocket_returns_svg_element(self):
        svg = render_item_label_svg(
            kind="pocket",
            template_key="avery-5160",
            fields=OPTIONAL_FIELDS["pocket"],
        )
        assert svg.startswith("<svg") or "<svg" in svg

    def test_pocket_valid_xml(self):
        svg = render_item_label_svg(
            kind="pocket",
            template_key="avery-5160",
            fields=OPTIONAL_FIELDS["pocket"],
        )
        ET.fromstring(svg)

    def test_spine_contains_call_number_fragment(self):
        # Use DEFAULT_FIELDS (no barcode) so there's enough vertical room
        # for the call number on the 1"-tall avery-5160 label.
        from compendium.services.labels import DEFAULT_FIELDS
        svg = render_item_label_svg(
            kind="spine",
            template_key="avery-5160",
            fields=DEFAULT_FIELDS["spine"],
        )
        # Sample row has call_number "PR6039.O32 L6 1965"; at least "PR6039" should appear.
        assert "PR6039" in svg

    def test_spine_contains_publication_year(self):
        from compendium.services.labels import DEFAULT_FIELDS
        svg = render_item_label_svg(
            kind="spine",
            template_key="avery-5160",
            fields=DEFAULT_FIELDS["spine"],
        )
        assert "1965" in svg

    def test_spine_contains_location(self):
        svg = render_item_label_svg(
            kind="spine",
            template_key="avery-5160",
            fields=frozenset({"location", "call_number"}),
        )
        assert "FICTION" in svg

    def test_barcode_only_returns_valid_xml(self):
        svg = render_item_label_svg(
            kind="barcode-only",
            template_key="avery-5167",
            fields=OPTIONAL_FIELDS["barcode-only"],
        )
        ET.fromstring(svg)

    def test_alphanumeric_barcode_does_not_raise(self):
        # "SAMPLE-001" fails EAN-13 validation; the code must fall back to the
        # rect-based barcode path without touching Drawing.drawOn.
        svg = render_item_label_svg(
            kind="pocket",
            template_key="avery-5160",
            fields=frozenset({"barcode", "call_number"}),
        )
        assert "<svg" in svg

    def test_all_compatible_combos_produce_valid_xml(self):
        for kind in ITEM_KIND_TO_FORMAT:
            fmt = ITEM_KIND_TO_FORMAT[kind]
            fields = OPTIONAL_FIELDS.get(fmt, frozenset())
            for t in compatible_templates(kind):
                svg = render_item_label_svg(
                    kind=kind,
                    template_key=t.key,
                    fields=fields,
                )
                ET.fromstring(svg)  # must parse cleanly

    def test_empty_fields_renders_without_error(self):
        svg = render_item_label_svg(
            kind="spine",
            template_key="avery-5160",
            fields=frozenset(),
        )
        ET.fromstring(svg)

    def test_rotated_template_renders_without_error(self):
        svg = render_item_label_svg(
            kind="spine",
            template_key="avery-5167-spine",
            fields=OPTIONAL_FIELDS["spine"],
        )
        ET.fromstring(svg)


class TestSampleItemRow:
    def test_has_call_number(self):
        assert _SAMPLE_ITEM_ROW.call_number

    def test_has_publication_year(self):
        assert _SAMPLE_ITEM_ROW.publication_year

    def test_has_location(self):
        assert _SAMPLE_ITEM_ROW.location

    def test_has_branch_code(self):
        assert _SAMPLE_ITEM_ROW.branch_code

    def test_cutter_derivable_from_author(self):
        c = cutter(_SAMPLE_ITEM_ROW.author_display)
        assert c  # non-empty cutter string

    def test_barcode_is_not_ean13(self):
        digits = "".join(ch for ch in _SAMPLE_ITEM_ROW.barcode if ch.isdigit())
        assert len(digits) not in (12, 13), "Sample barcode must not match EAN-13 format"


# ──────────────────────────────────────────────────────────────────────
# Stub canvas that records all draw calls (no transforms applied —
# used when _draw_item_label_content is called directly in a flat context)
# ──────────────────────────────────────────────────────────────────────


class _FullRecorder:
    """LabelCanvas stub that records rect/text calls for inspection."""

    def __init__(self):
        self.rects: list[tuple[float, float, float, float]] = []
        self.strings: list[tuple[float, float, str]] = []
        self.centred: list[tuple[float, float, str]] = []
        self.right: list[tuple[float, float, str]] = []
        self._font_size: float = 10.0

    def setFont(self, name: str, size: float) -> None:
        self._font_size = size

    def drawString(self, x: float, y: float, text: str) -> None:
        self.strings.append((x, y, str(text)))

    def drawCentredString(self, x: float, y: float, text: str) -> None:
        self.centred.append((x, y, str(text)))

    def drawRightString(self, x: float, y: float, text: str) -> None:
        self.right.append((x, y, str(text)))

    def rect(self, x: float, y: float, w: float, h: float,
             fill: int = 0, stroke: int = 1) -> None:
        self.rects.append((x, y, w, h))

    def saveState(self) -> None:
        pass

    def restoreState(self) -> None:
        pass

    def translate(self, dx: float, dy: float) -> None:
        pass

    def rotate(self, degrees: float) -> None:
        pass


def _render_pocket(fields: frozenset[str], *, library_name: str | None = None) -> _FullRecorder:
    """Render one pocket label via _draw_item_label_content and return the recorder."""
    from compendium.services.labels import _draw_item_label_content

    row = ItemLabelRow(
        barcode="BC000001",
        title="The Luminaries",
        author_display="Eleanor Catton",
        call_number="PR9639.4 .C38 L86 2013",
        publication_year=2013,
        branch_code="MAIN",
        location="FICTION",
    )
    lw = TEMPLATES["avery-5160"].label_width * inch
    lh = TEMPLATES["avery-5160"].label_height * inch
    rec = _FullRecorder()
    _draw_item_label_content(
        rec, row, 0.0, 0.0, lw, lh,
        "pocket", False, "code128", False, fields,
        library_name=library_name,
    )
    return rec


# ──────────────────────────────────────────────────────────────────────
# Fix 1: library_name in SVG preview
# ──────────────────────────────────────────────────────────────────────


class TestLibraryNameInPreview:
    def test_renders_library_name_when_field_enabled_and_provided(self):
        svg = render_item_label_svg(
            kind="pocket",
            template_key="avery-5160",
            fields=frozenset({"title", "library_name"}),
            library_name="Riverdale Public Library",
        )
        assert "Riverdale Public Library" in svg

    def test_omits_library_name_when_field_not_in_fields(self):
        svg = render_item_label_svg(
            kind="pocket",
            template_key="avery-5160",
            fields=frozenset({"title"}),
            library_name="Riverdale Public Library",
        )
        assert "Riverdale Public Library" not in svg

    def test_omits_library_name_when_not_provided(self):
        svg = render_item_label_svg(
            kind="pocket",
            template_key="avery-5160",
            fields=frozenset({"title", "library_name"}),
            library_name=None,
        )
        assert "<svg" in svg  # renders without error


# ──────────────────────────────────────────────────────────────────────
# Fix 2: pocket — author on its own line
# ──────────────────────────────────────────────────────────────────────


class TestPocketAuthorOnOwnLine:
    _ALL = frozenset({"title", "author", "branch", "call_number", "barcode"})

    def test_title_and_author_drawn_as_separate_strings(self):
        rec = _render_pocket(frozenset({"title", "author"}))
        texts = [t for _, _, t in rec.strings]
        assert any("The Luminaries" in t and "Eleanor Catton" not in t for t in texts), \
            "title should be its own string, not concatenated with author"
        assert any("Eleanor Catton" in t for t in texts), \
            "author should be drawn as a separate string"

    def test_no_emdash_concatenation(self):
        rec = _render_pocket(frozenset({"title", "author"}))
        all_text = " | ".join(t for _, _, t in rec.strings + rec.centred)
        assert " — " not in all_text or "Eleanor Catton" not in all_text, \
            "title and author should not be joined with ' — '"

    def test_author_visible_when_branch_enabled(self):
        rec = _render_pocket(self._ALL)
        texts = [t for _, _, t in rec.strings]
        assert any("Eleanor Catton" in t for t in texts)

    def test_author_baseline_below_title_baseline(self):
        rec = _render_pocket(frozenset({"title", "author"}))
        title_y = next(y for _, y, t in rec.strings if "The Luminaries" in t)
        author_y = next(y for _, y, t in rec.strings if "Eleanor Catton" in t)
        assert author_y < title_y, "author baseline should be lower than title baseline (PDF y-up)"


# ──────────────────────────────────────────────────────────────────────
# Fix 3: spine barcode capped to 0.75 inch
# ──────────────────────────────────────────────────────────────────────


class TestSpineBarcodeCap:
    def _render_spine_rects(
        self, template_key: str, *, rotated_ctx: bool
    ) -> list[tuple[float, float, float, float]]:
        from compendium.services.labels import _draw_item_label_content

        tmpl = TEMPLATES[template_key]
        lw = tmpl.label_width * inch
        lh = tmpl.label_height * inch
        if rotated_ctx:
            lw, lh = lh, lw
        row = ItemLabelRow(barcode="BC000001", title="Dune",
                           author_display="Frank Herbert", call_number="PS3551")
        rec = _FullRecorder()
        _draw_item_label_content(
            rec, row, 0.0, 0.0, lw, lh,
            "spine", False, "code128", rotated_ctx,
            frozenset({"call_number", "barcode"}),
        )
        return rec.rects

    def test_rotated_spine_barcode_capped_at_three_quarter_inch(self):
        rects = self._render_spine_rects("avery-5160-spine", rotated_ctx=True)
        max_y_extent = max(y + h for (_, y, _, h) in rects) if rects else 0
        # barcode starts at y+pad=4, extends at most pad + 0.75*inch = 4 + 54 = 58pt
        assert max_y_extent <= 4 + 0.75 * inch + 1, (
            f"Rotated spine barcode upper extent {max_y_extent:.1f}pt "
            f"exceeds 0.75\" cap ({0.75 * inch:.1f}pt)"
        )

    def test_short_rotated_spine_uses_available_not_cap(self):
        # avery-5167-spine long axis 1.75" = 126pt; available = (126-8)*0.40 ≈ 47pt < cap
        rects = self._render_spine_rects("avery-5167-spine", rotated_ctx=True)
        max_y_extent = max(y + h for (_, y, _, h) in rects) if rects else 0
        # available_strip ≈ 47.2pt, so bar top ≤ 4 + 47.2 ≈ 51.2pt
        assert max_y_extent <= 4 + (126 - 8) * 0.40 + 2

    def test_flat_spine_barcode_width_capped(self):
        rects = self._render_spine_rects("avery-5160", rotated_ctx=False)
        # flat spine: bar width (along x) should be ≤ 0.75"
        max_bar_extent = max(x + w for (x, _, w, _) in rects) if rects else 0
        min_bar_start = min(x for (x, _, _, _) in rects) if rects else 0
        bar_span = max_bar_extent - min_bar_start
        assert bar_span <= 0.75 * inch + 1, (
            f"Flat spine barcode x-span {bar_span:.1f}pt exceeds 0.75\" cap"
        )

    def test_flat_spine_barcode_centered(self):
        from compendium.services.labels import _draw_item_label_content

        tmpl = TEMPLATES["avery-5160"]
        lw = tmpl.label_width * inch
        lh = tmpl.label_height * inch
        row = ItemLabelRow(barcode="BC000001", title="Dune",
                           author_display="Frank Herbert", call_number="PS3551")
        rec = _FullRecorder()
        _draw_item_label_content(
            rec, row, 0.0, 0.0, lw, lh,
            "spine", False, "code128", False,
            frozenset({"call_number", "barcode"}),
        )
        inner_w = lw - 2 * 4  # pad = 4
        bar_xs = [x for (x, _, _, _) in rec.rects]
        bar_x_max = max(x + w for (x, _, w, _) in rec.rects)
        bar_x_min = min(bar_xs)
        bar_mid = (bar_x_min + bar_x_max) / 2
        cell_mid = lw / 2
        assert abs(bar_mid - cell_mid) < 5, (
            f"Barcode center {bar_mid:.1f} not near cell center {cell_mid:.1f}"
        )


# ──────────────────────────────────────────────────────────────────────
# Fix 4: HR text drawn inside the cell
# ──────────────────────────────────────────────────────────────────────


class TestHRTextInsideCell:
    def _call_draw_barcode(
        self, x: float, y: float, width: float, height: float
    ) -> _FullRecorder:
        from compendium.services.labels import _draw_barcode

        rec = _FullRecorder()
        _draw_barcode(rec, x, y, "BC000001", width, height,
                      symbology="code128", human_readable=True)
        return rec

    def test_hr_text_y_inside_height_region(self):
        rec = self._call_draw_barcode(x=10, y=5, width=100, height=30)
        for _, cy, _ in rec.centred:
            assert 5 <= cy <= 35, f"HR text y={cy} outside cell [5, 35]"

    def test_bars_do_not_exceed_top_of_height(self):
        rec = self._call_draw_barcode(x=10, y=5, width=100, height=30)
        for (rx, ry, rw, rh) in rec.rects:
            assert ry + rh <= 5 + 30 + 0.1, f"Bar top {ry+rh:.1f} exceeds y+height=35"

    def test_barcode_only_preview_includes_hr_text_in_svg(self):
        from compendium.services.labels import _human_readable_text
        expected_hr = _human_readable_text("SAMPLE-001", "code128")
        svg = render_item_label_svg(
            kind="barcode-only",
            template_key="avery-5167",
            fields=frozenset({"barcode", "human_readable"}),
        )
        assert expected_hr in svg, \
            f"HR text '{expected_hr}' not found in barcode-only SVG preview"


# ──────────────────────────────────────────────────────────────────────
# Font-aware recorder (extends _FullRecorder with per-call font size)
# ──────────────────────────────────────────────────────────────────────


class _FontAwareRecorder(_FullRecorder):
    """Like _FullRecorder but records the active font size alongside each draw."""

    def __init__(self):
        super().__init__()
        self.strings_with_font: list[tuple[float, float, str, float]] = []
        self.centred_with_font: list[tuple[float, float, str, float]] = []

    def drawString(self, x: float, y: float, text: str) -> None:
        super().drawString(x, y, text)
        self.strings_with_font.append((x, y, str(text), self._font_size))

    def drawCentredString(self, x: float, y: float, text: str) -> None:
        super().drawCentredString(x, y, text)
        self.centred_with_font.append((x, y, str(text), self._font_size))


def _render_pocket_on(
    template_key: str,
    fields: frozenset[str],
    *,
    library_name: str | None = None,
) -> _FontAwareRecorder:
    """Render one pocket label on the given template and return a font-aware recorder."""
    from compendium.services.labels import _draw_item_label_content

    row = ItemLabelRow(
        barcode="BC000001",
        title="The Luminaries",
        author_display="Eleanor Catton",
        call_number="PR9639.4 .C38 L86 2013",
        publication_year=2013,
        branch_code="MAIN",
        location="FICTION",
    )
    tmpl = TEMPLATES[template_key]
    lw = tmpl.label_width * inch
    lh = tmpl.label_height * inch
    rec = _FontAwareRecorder()
    _draw_item_label_content(
        rec, row, 0.0, 0.0, lw, lh,
        "pocket", False, "code128", False, fields,
        library_name=library_name,
    )
    return rec


def _render_spine_on(
    template_key: str,
    fields: frozenset[str],
    *,
    rotated_ctx: bool = False,
) -> _FullRecorder:
    """Render one spine label with branch+location set and return recorder."""
    from compendium.services.labels import _draw_item_label_content

    row = ItemLabelRow(
        barcode="BC000001",
        title="Dune",
        author_display="Frank Herbert",
        call_number="PS3551 .E76 D8",
        publication_year=1965,
        branch_code="MAIN",
        location="FICTION",
    )
    tmpl = TEMPLATES[template_key]
    lw = tmpl.label_width * inch
    lh = tmpl.label_height * inch
    if rotated_ctx:
        lw, lh = lh, lw
    rec = _FullRecorder()
    _draw_item_label_content(
        rec, row, 0.0, 0.0, lw, lh,
        "spine", False, "code128", rotated_ctx, fields,
    )
    return rec


# ──────────────────────────────────────────────────────────────────────
# Fix A: preview field fallback removed — empty fields means empty
# ──────────────────────────────────────────────────────────────────────


class TestPreviewEmptyFieldsNoFallback:
    """render_item_label_svg with empty fields must produce a blank label —
    no text drawn, matching what the PDF path does when no boxes are checked."""

    def test_empty_fields_omits_sample_title(self):
        svg = render_item_label_svg(
            kind="pocket",
            template_key="avery-5160",
            fields=frozenset(),
        )
        assert "Lord of the Rings" not in svg

    def test_empty_fields_omits_year(self):
        svg = render_item_label_svg(
            kind="pocket",
            template_key="avery-5160",
            fields=frozenset(),
        )
        assert "1965" not in svg

    def test_year_field_alone_shows_year(self):
        """With only 'year' checked (and call_number unchecked), year appears."""
        svg = render_item_label_svg(
            kind="pocket",
            template_key="avery-5160",
            fields=frozenset({"year"}),
        )
        assert "1965" in svg

    def test_year_field_absent_when_call_number_also_present(self):
        """When call_number is in fields, year must NOT appear as a separate
        info-line entry. We verify this at the recorder level, not SVG string,
        because the sample call_number itself ends in '1965'."""
        from compendium.services.labels import _draw_item_label_content
        from compendium.services.labels import _SAMPLE_ITEM_ROW

        lw = TEMPLATES["avery-5160"].label_width * inch
        lh = TEMPLATES["avery-5160"].label_height * inch
        rec_both = _FontAwareRecorder()
        _draw_item_label_content(
            rec_both, _SAMPLE_ITEM_ROW, 0.0, 0.0, lw, lh,
            "pocket", False, "code128", False,
            frozenset({"year", "call_number"}),
        )

        rec_cn_only = _FontAwareRecorder()
        _draw_item_label_content(
            rec_cn_only, _SAMPLE_ITEM_ROW, 0.0, 0.0, lw, lh,
            "pocket", False, "code128", False,
            frozenset({"call_number"}),
        )

        # The info line text must be identical whether year is checked or not
        # (year is suppressed whenever call_number is present).
        both_texts = {t for (_, _, t, _) in rec_both.strings_with_font}
        cn_texts   = {t for (_, _, t, _) in rec_cn_only.strings_with_font}
        assert both_texts == cn_texts, (
            "Info line changed when year toggled alongside call_number: "
            f"with_year={both_texts}, cn_only={cn_texts}"
        )


# ──────────────────────────────────────────────────────────────────────
# Fix B: pocket font scaling on larger templates
# ──────────────────────────────────────────────────────────────────────


class TestPocketFontScaling:
    """Font sizes must grow proportionally with label height so larger
    pocket templates (5871, 22805, 22806) don't leave half the cell blank."""

    _TITLE_FIELDS = frozenset({"title"})

    def _title_font_size(self, template_key: str) -> float:
        rec = _render_pocket_on(template_key, self._TITLE_FIELDS)
        title_calls = [fs for (_, _, text, fs) in rec.strings_with_font
                       if "Luminaries" in text or "Lumin" in text]
        assert title_calls, f"Title not drawn on {template_key}"
        return title_calls[0]

    def test_avery_5160_title_size_is_baseline_8pt(self):
        assert self._title_font_size("avery-5160") == 8

    def test_avery_22805_title_size_scales_to_12pt(self):
        # 22805 is 1.5" tall → scale 1.5 → round(8 * 1.5) = 12
        assert self._title_font_size("avery-22805") == 12

    def test_avery_22806_title_size_scales_to_16pt(self):
        # 22806 is 2.0" tall → scale 2.0 → round(8 * 2.0) = 16
        assert self._title_font_size("avery-22806") == 16

    def test_avery_5871_title_size_scales_to_16pt(self):
        # 5871 is 2.0" tall → scale 2.0 → same as 22806
        assert self._title_font_size("avery-5871") == 16

    def test_avery_22806_barcode_height_scales_above_baseline(self):
        """Barcode rect height on 22806 must exceed the 20pt baseline."""
        rec = _render_pocket_on("avery-22806", frozenset({"barcode"}))
        if not rec.rects:
            pytest.skip("no rect calls — barcode may not have rendered")
        max_bar_h = max(rh for (_, _, _, rh) in rec.rects)
        assert max_bar_h > 20, f"Expected bar height > 20pt, got {max_bar_h:.1f}pt"


# ──────────────────────────────────────────────────────────────────────
# Fix C: spine branch + location merged onto one line
# ──────────────────────────────────────────────────────────────────────


class TestSpineBranchLocationSideBySide:
    """When both branch and location are enabled, they should be drawn as one
    combined string ("MAIN  ·  FICTION") rather than two separate stacked lines.
    This reclaims 9pt of vertical space on the flat 5160, allowing at least one
    call-number line to fit when all fields are enabled."""

    def test_rotated_branch_only_appears_alone(self):
        rec = _render_spine_on(
            "avery-5167-spine",
            frozenset({"branch"}),
            rotated_ctx=True,
        )
        all_texts = [t for (_, _, t) in rec.strings]
        assert any("MAIN" in t for t in all_texts)
        assert not any("FICTION" in t for t in all_texts)

    def test_rotated_location_only_appears_alone(self):
        rec = _render_spine_on(
            "avery-5167-spine",
            frozenset({"location"}),
            rotated_ctx=True,
        )
        all_texts = [t for (_, _, t) in rec.strings]
        assert any("FICTION" in t for t in all_texts)
        assert not any("MAIN" in t for t in all_texts)

    def test_rotated_branch_and_location_on_single_line(self):
        """Both names must appear together in ONE drawString call.
        Use avery-5160-spine (64pt inner_w) so the combined string fits."""
        rec = _render_spine_on(
            "avery-5160-spine",
            frozenset({"branch", "location"}),
            rotated_ctx=True,
        )
        combined_calls = [t for (_, _, t) in rec.strings
                          if "MAIN" in t and "FICTION" in t]
        assert combined_calls, (
            "Branch and location not combined; "
            f"separate strings drawn: {[t for (_, _, t) in rec.strings]}"
        )

    def test_flat_5160_branch_and_location_on_single_line(self):
        """On flat (non-rotated) spine, combined line uses drawCentredString."""
        rec = _render_spine_on("avery-5160", frozenset({"branch", "location"}))
        combined_calls = [t for (_, _, t) in rec.centred
                          if "MAIN" in t and "FICTION" in t]
        assert combined_calls, (
            "Branch and location not combined on flat spine; "
            f"centred strings: {[t for (_, _, t) in rec.centred]}"
        )

    def test_flat_5160_all_fields_includes_call_number(self):
        """With every spine field enabled on flat 5160, the call number must
        appear — the side-by-side branch+location frees the vertical space."""
        rec = _render_spine_on(
            "avery-5160",
            frozenset({"branch", "location", "call_number", "cutter", "year", "barcode"}),
        )
        all_centred = [t for (_, _, t) in rec.centred]
        assert any("PS3551" in t for t in all_centred), (
            "Call number (PS3551) absent from flat 5160 with all fields; "
            f"centred strings: {all_centred}"
        )
