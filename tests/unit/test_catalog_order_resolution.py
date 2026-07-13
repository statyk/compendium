"""Default-sort resolution for catalog searches (UX slice 7)."""
from compendium.web.routes.catalog import _resolve_order_by


def test_keyword_all_fields_defaults_to_relevance():
    assert _resolve_order_by("", "dune", "all") == "relevance"


def test_browse_defaults_to_title():
    assert _resolve_order_by("", "", "all") == "title"
    assert _resolve_order_by("", "   ", "all") == "title"


def test_field_scoped_defaults_to_title():
    assert _resolve_order_by("", "dune", "title") == "title"


def test_explicit_choice_respected():
    assert _resolve_order_by("title", "dune", "all") == "title"
    assert _resolve_order_by("recent", "dune", "all") == "recent"
    assert _resolve_order_by("relevance", "dune", "all") == "relevance"


def test_relevance_coerced_to_title_off_the_fts_path():
    assert _resolve_order_by("relevance", "dune", "title") == "title"
    assert _resolve_order_by("relevance", "", "all") == "title"


def test_garbage_falls_back_to_default():
    assert _resolve_order_by("bogus", "dune", "all") == "relevance"
    assert _resolve_order_by("bogus", "", "all") == "title"
