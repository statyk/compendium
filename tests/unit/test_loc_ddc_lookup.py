"""Unit tests for Library of Congress DDC fallback lookup."""

from unittest.mock import MagicMock, patch

from compendium.services.metadata import (
    _parse_ddc_from_marcxml,
    _try_ddc_by_isbn,
    _try_ddc_by_lccn,
    lookup_ddc_from_loc,
)

_LCCN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<marc:record xmlns:marc="http://www.loc.gov/MARC21/slim">
  <marc:datafield tag="082" ind1="0" ind2="4">
    <marc:subfield code="a">813.54</marc:subfield>
  </marc:datafield>
</marc:record>"""

_SRU_XML = """<?xml version="1.0" encoding="UTF-8"?>
<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
  <srw:records>
    <srw:record>
      <srw:recordData>
        <record xmlns="http://www.loc.gov/MARC21/slim">
          <datafield tag="082" ind1="0" ind2="4">
            <subfield code="a">813.54</subfield>
          </datafield>
        </record>
      </srw:recordData>
    </srw:record>
  </srw:records>
</srw:searchRetrieveResponse>"""

_NO_082_XML = """<?xml version="1.0" encoding="UTF-8"?>
<marc:record xmlns:marc="http://www.loc.gov/MARC21/slim">
  <marc:datafield tag="050" ind1=" " ind2="0">
    <marc:subfield code="a">PS3558.E63</marc:subfield>
  </marc:datafield>
</marc:record>"""

_BOTH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<marc:record xmlns:marc="http://www.loc.gov/MARC21/slim">
  <marc:datafield tag="050" ind1=" " ind2="0">
    <marc:subfield code="a">PS3558.E63</marc:subfield>
  </marc:datafield>
  <marc:datafield tag="082" ind1="0" ind2="4">
    <marc:subfield code="a">813.54</marc:subfield>
  </marc:datafield>
</marc:record>"""


def test_parse_ddc_from_lccn_response():
    assert _parse_ddc_from_marcxml(_LCCN_XML) == "813.54"


def test_parse_ddc_from_sru_response():
    assert _parse_ddc_from_marcxml(_SRU_XML) == "813.54"


def test_parse_ddc_returns_none_when_no_082():
    assert _parse_ddc_from_marcxml(_NO_082_XML) is None


def test_parse_ddc_ignores_050_returns_082():
    assert _parse_ddc_from_marcxml(_BOTH_XML) == "813.54"


def test_parse_ddc_returns_none_on_bad_xml():
    assert _parse_ddc_from_marcxml("not xml {{{{") is None


def test_try_ddc_by_lccn_returns_number_on_success():
    mock_resp = MagicMock(status_code=200, text=_LCCN_XML)
    with patch("compendium.services.metadata.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.get.return_value = mock_resp
        assert _try_ddc_by_lccn("65012174") == "813.54"


def test_try_ddc_by_lccn_returns_none_on_404():
    mock_resp = MagicMock(status_code=404, text="")
    with patch("compendium.services.metadata.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.get.return_value = mock_resp
        assert _try_ddc_by_lccn("badlccn") is None


def test_try_ddc_by_lccn_returns_none_on_network_error():
    with patch("compendium.services.metadata.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.get.side_effect = Exception("timeout")
        assert _try_ddc_by_lccn("65012174") is None


def test_try_ddc_by_isbn_returns_number_on_success():
    mock_resp = MagicMock(status_code=200, text=_SRU_XML)
    with patch("compendium.services.metadata.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.get.return_value = mock_resp
        assert _try_ddc_by_isbn("9780441013593") == "813.54"


def test_try_ddc_by_isbn_returns_none_on_failure():
    with patch("compendium.services.metadata.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.get.side_effect = Exception("timeout")
        assert _try_ddc_by_isbn("9780441013593") is None


def test_lookup_ddc_uses_lccn_first():
    with patch("compendium.services.metadata._try_ddc_by_lccn", return_value="813.54") as mock_lccn, \
         patch("compendium.services.metadata._try_ddc_by_isbn") as mock_isbn:
        result = lookup_ddc_from_loc("9780441013593", lccn="65012174")
    assert result == "813.54"
    mock_lccn.assert_called_once_with("65012174")
    mock_isbn.assert_not_called()


def test_lookup_ddc_falls_back_to_isbn_when_lccn_fails():
    with patch("compendium.services.metadata._try_ddc_by_lccn", return_value=None), \
         patch("compendium.services.metadata._try_ddc_by_isbn", return_value="813.54") as mock_isbn:
        result = lookup_ddc_from_loc("9780441013593", lccn="65012174")
    assert result == "813.54"
    mock_isbn.assert_called_once_with("9780441013593")


def test_lookup_ddc_uses_isbn_when_no_lccn():
    with patch("compendium.services.metadata._try_ddc_by_lccn") as mock_lccn, \
         patch("compendium.services.metadata._try_ddc_by_isbn", return_value="813.54"):
        result = lookup_ddc_from_loc("9780441013593", lccn=None)
    mock_lccn.assert_not_called()
    assert result == "813.54"


def test_lookup_ddc_returns_none_when_all_fail():
    with patch("compendium.services.metadata._try_ddc_by_lccn", return_value=None), \
         patch("compendium.services.metadata._try_ddc_by_isbn", return_value=None):
        assert lookup_ddc_from_loc("9780441013593", lccn="65012174") is None
