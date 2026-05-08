"""Unit tests for services._normalization helpers."""

import pytest

from compendium.services._normalization import (
    compute_sort_title,
    normalize_creator_name,
    normalize_title,
)


class TestComputeSortTitle:
    def test_strips_the(self):
        assert compute_sort_title("The Great Gatsby") == "Great Gatsby"

    def test_strips_an(self):
        assert compute_sort_title("An Anthropologist on Mars") == "Anthropologist on Mars"

    def test_strips_a(self):
        assert compute_sort_title("A Tale of Two Cities") == "Tale of Two Cities"

    def test_case_insensitive_the(self):
        assert compute_sort_title("the lower case") == "lower case"

    def test_case_insensitive_an(self):
        assert compute_sort_title("AN UPPERCASE") == "UPPERCASE"

    def test_no_article(self):
        assert compute_sort_title("Foundation") == "Foundation"

    def test_theatre_not_stripped(self):
        # "Theatre" starts with "The" but has no trailing space after "The"
        assert compute_sort_title("Theatre") == "Theatre"

    def test_anthology_not_stripped(self):
        # "Anthology" starts with "An" but no trailing space
        assert compute_sort_title("Anthology") == "Anthology"

    def test_empty(self):
        assert compute_sort_title("") == ""

    def test_preserves_rest_casing(self):
        result = compute_sort_title("The GREAT Gatsby")
        assert result == "GREAT Gatsby"


class TestNormalizeTitle:
    def test_trailing_the(self):
        assert normalize_title("Information, The") == "The Information"

    def test_trailing_a(self):
        assert normalize_title("Tale, A") == "A Tale"

    def test_trailing_an(self):
        assert normalize_title("Odd Man Out, An") == "An Odd Man Out"

    def test_non_article_unchanged(self):
        assert normalize_title("Smith, John") == "Smith, John"

    def test_no_comma_unchanged(self):
        assert normalize_title("Foundation") == "Foundation"

    def test_already_leading_article(self):
        assert normalize_title("The Information") == "The Information"

    def test_empty(self):
        assert normalize_title("") == ""

    def test_case_insensitive_suffix(self):
        assert normalize_title("Information, THE") == "THE Information"


class TestNormalizeCreatorName:
    def test_last_first(self):
        assert normalize_creator_name("Brooks, David") == "David Brooks"

    def test_last_first_with_middle(self):
        assert normalize_creator_name("Smith, John A.") == "John A. Smith"

    def test_suffix_jr_unchanged(self):
        assert normalize_creator_name("Smith, Jr.") == "Smith, Jr."

    def test_suffix_sr_unchanged(self):
        assert normalize_creator_name("Jones, Sr.") == "Jones, Sr."

    def test_suffix_ii_unchanged(self):
        assert normalize_creator_name("Smith, II") == "Smith, II"

    def test_suffix_phd_unchanged(self):
        assert normalize_creator_name("Jones, PhD") == "Jones, PhD"

    def test_multiple_commas_unchanged(self):
        # e.g. "Smith, John, ed." — ambiguous, leave alone
        assert normalize_creator_name("Smith, John, ed.") == "Smith, John, ed."

    def test_no_comma_unchanged(self):
        assert normalize_creator_name("Aristotle") == "Aristotle"

    def test_single_name_unchanged(self):
        assert normalize_creator_name("Madonna") == "Madonna"

    def test_whitespace_trimmed(self):
        assert normalize_creator_name("  Brooks ,  David ") == "David Brooks"

    def test_empty_unchanged(self):
        assert normalize_creator_name("") == ""

    def test_already_first_last_unchanged(self):
        # "David Brooks" has no comma, returned as-is
        assert normalize_creator_name("David Brooks") == "David Brooks"
