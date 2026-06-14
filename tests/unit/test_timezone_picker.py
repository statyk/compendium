"""Unit tests for the IANA timezone grouping used by the settings picker."""

from __future__ import annotations

from zoneinfo import available_timezones

from compendium.web.jinja import _jinja_iana_timezone_groups


def test_groups_cover_every_iana_zone():
    grouped = {
        value
        for _region, zones in _jinja_iana_timezone_groups()
        for value, _label in zones
    }
    assert grouped == set(available_timezones())


def test_regions_sorted_and_zones_sorted_by_label():
    groups = _jinja_iana_timezone_groups()
    regions = [r for r, _ in groups]
    assert regions == sorted(regions)
    for _region, zones in groups:
        labels = [label for _value, label in zones]
        assert labels == sorted(labels)


def test_slashless_zones_grouped_under_general_with_self_label():
    groups = dict(_jinja_iana_timezone_groups())
    assert "General" in groups
    general = dict(groups["General"])
    assert general.get("UTC") == "UTC"


def test_region_labels_strip_prefix_and_underscores():
    groups = dict(_jinja_iana_timezone_groups())
    america = dict(groups["America"])
    # value keeps full IANA name; label drops region prefix + underscores.
    assert america["America/New_York"] == "New York"
