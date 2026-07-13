"""today_iso() must follow the library_timezone setting (UX slice 7)."""
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from compendium.web.jinja import _jinja_today_iso


def test_today_iso_uses_library_timezone():
    with patch("compendium.web.jinja.get_site_setting", return_value="Pacific/Kiritimati"):
        east = _jinja_today_iso()
    with patch("compendium.web.jinja.get_site_setting", return_value="Etc/GMT+12"):
        west = _jinja_today_iso()
    assert east == datetime.now(ZoneInfo("Pacific/Kiritimati")).date().isoformat()
    assert west == datetime.now(ZoneInfo("Etc/GMT+12")).date().isoformat()


def test_today_iso_unknown_zone_falls_back_to_utc():
    with patch("compendium.web.jinja.get_site_setting", return_value="Not/AZone"):
        assert len(_jinja_today_iso()) == 10  # renders, no raise
