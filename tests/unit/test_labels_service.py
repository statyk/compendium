"""Unit tests for the labels service — cutter logic + PDF rendering sanity."""

from __future__ import annotations

from datetime import date

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
