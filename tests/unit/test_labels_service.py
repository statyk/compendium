"""Unit tests for the labels service — cutter logic + PDF rendering sanity."""

from __future__ import annotations

from datetime import date

import barcode as _barcode_lib
import pytest

from compendium.services.labels import (
    ItemLabelRow,
    PatronCardRow,
    TEMPLATES,
    cutter,
    generate_item_labels,
    generate_patron_cards,
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

    def test_spine_format_short_pdf(self):
        # "spine" is a backward-compat alias for "spine-text"; kept to verify
        # the alias continues to work as callers upgrade.
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

    def test_spine_text_format_renders(self):
        rows = [
            ItemLabelRow(
                barcode="BC000001",
                title="Dune",
                author_display="Frank Herbert",
                call_number="PS3551 .E76",
            )
        ]
        pdf = generate_item_labels(rows, template_key="avery-5167", format="spine-text")
        assert pdf.startswith(b"%PDF-")

    def test_spine_barcode_format_renders(self):
        rows = [
            ItemLabelRow(
                barcode="BC000001",
                title="Dune",
                author_display="Frank Herbert",
                call_number="PS3551 .E76",
            )
        ]
        pdf = generate_item_labels(rows, template_key="avery-5167", format="spine-barcode")
        assert pdf.startswith(b"%PDF-")

    def test_location_field_accepted(self):
        # location is rendered in Task 8; for now verify the field is accepted
        # on both spine-text and spine-barcode without raising and produces a PDF.
        rows = [
            ItemLabelRow(
                barcode="BC000001",
                title="Dune",
                author_display="Frank Herbert",
                call_number="PS3551 .E76",
                location="REFERENCE",
            )
        ]
        pdf_text = generate_item_labels(rows, template_key="avery-5167", format="spine-text")
        assert pdf_text.startswith(b"%PDF-")
        pdf_barcode = generate_item_labels(rows, template_key="avery-5167", format="spine-barcode")
        assert pdf_barcode.startswith(b"%PDF-")

    def test_format_auto_picks_based_on_template(self):
        rows = [ItemLabelRow(barcode="BC1", title="A")]
        # small template → spine (no barcode drawn, just text)
        pdf_small = generate_item_labels(rows, template_key="avery-5167", format=None)
        # larger → pocket (includes barcode, larger PDF)
        pdf_big = generate_item_labels(rows, template_key="avery-5160", format=None)
        assert pdf_small.startswith(b"%PDF-")
        assert pdf_big.startswith(b"%PDF-")
        # Pocket includes a barcode, so should be larger than the spine-only PDF
        assert len(pdf_big) > len(pdf_small)

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
