"""Unit tests for the by-country timezone picker data."""

from __future__ import annotations

from compendium.web.jinja import _jinja_timezone_picker_data
from compendium.web.tz_regions import TZ_REGIONS


def _all_zones() -> set[str]:
    return {zone for _label, zones in TZ_REGIONS for zone, _city in zones}


def test_utc_and_anglophone_countries_pinned_first():
    labels = [label for label, _ in TZ_REGIONS]
    # UTC first, then the anglophone-priority countries, before the separator.
    sep = labels.index("──────────")
    pinned = labels[:sep]
    assert pinned[0] == "UTC"
    assert pinned[1:] == ["United States", "Canada", "United Kingdom",
                          "Ireland", "Australia", "New Zealand"]


def test_rest_after_separator_is_alphabetical():
    labels = [label for label, _ in TZ_REGIONS]
    sep = labels.index("──────────")
    rest = labels[sep + 1:]
    assert rest == sorted(rest, key=str.lower)


def test_us_zones_grouped_under_united_states():
    groups = dict(TZ_REGIONS)
    us = {zone for zone, _ in groups["United States"]}
    assert "America/New_York" in us
    assert "America/Los_Angeles" in us
    assert "Pacific/Honolulu" in us            # a US zone outside America/
    assert "America/Indiana/Indianapolis" in us
    # Brazil's zone must not leak into the US group.
    assert "America/Sao_Paulo" not in us


def test_south_american_zone_under_its_own_country():
    groups = dict(TZ_REGIONS)
    assert ("America/Sao_Paulo", "Sao Paulo") in groups["Brazil"]


def test_separator_has_no_zones():
    groups = dict(TZ_REGIONS)
    assert groups["──────────"] == []


def test_selected_region_resolves_country_for_known_zone():
    data = _jinja_timezone_picker_data("America/Chicago")
    assert data["selected_region"] == "United States"


def test_unknown_value_is_injected_as_current_group():
    # A legacy alias not in the canonical list must still be selectable.
    data = _jinja_timezone_picker_data("US/Eastern")
    assert data["selected_region"] == "Current"
    assert data["groups"][0] == ("Current", [("US/Eastern", "US/Eastern")])
    # ...and the original groups are preserved after it.
    assert data["groups"][1][0] == "UTC"


def test_none_value_defaults_to_first_group():
    data = _jinja_timezone_picker_data(None)
    assert data["selected_region"] == "UTC"
