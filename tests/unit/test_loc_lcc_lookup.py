"""Unit tests for Library of Congress LCC fallback lookup."""

from unittest.mock import MagicMock, patch

from compendium.services.metadata import (
    _parse_lcc_from_marcxml,
    _try_lcc_by_isbn,
    _try_lcc_by_lccn,
    lookup_lcc_from_loc,
    parse_open_library,
)

# ---------------------------------------------------------------------------
# MARC XML samples
# ---------------------------------------------------------------------------

# LCCN permalink response (marc: prefix namespace)
_LCCN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<marc:record xmlns:marc="http://www.loc.gov/MARC21/slim">
  <marc:datafield tag="050" ind1=" " ind2="0">
    <marc:subfield code="a">PS3558.E63</marc:subfield>
    <marc:subfield code="b">D8 1984</marc:subfield>
  </marc:datafield>
</marc:record>"""

# SRU response (default namespace, nested in SRW wrapper)
_SRU_XML = """<?xml version="1.0" encoding="UTF-8"?>
<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/">
  <srw:numberOfRecords>1</srw:numberOfRecords>
  <srw:records>
    <srw:record>
      <srw:recordData>
        <record xmlns="http://www.loc.gov/MARC21/slim">
          <datafield tag="050" ind1=" " ind2="4">
            <subfield code="a">PS3558.E63</subfield>
            <subfield code="b">D8</subfield>
          </datafield>
        </record>
      </srw:recordData>
    </srw:record>
  </srw:records>
</srw:searchRetrieveResponse>"""

_NO_050_XML = """<?xml version="1.0" encoding="UTF-8"?>
<marc:record xmlns:marc="http://www.loc.gov/MARC21/slim">
  <marc:datafield tag="245" ind1="1" ind2="0">
    <marc:subfield code="a">Dune</marc:subfield>
  </marc:datafield>
</marc:record>"""

_SUBFIELD_A_ONLY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<marc:record xmlns:marc="http://www.loc.gov/MARC21/slim">
  <marc:datafield tag="050" ind1=" " ind2="0">
    <marc:subfield code="a">PS3558.E63</marc:subfield>
  </marc:datafield>
</marc:record>"""


# ---------------------------------------------------------------------------
# _parse_lcc_from_marcxml
# ---------------------------------------------------------------------------

def test_parse_lccn_response():
    result = _parse_lcc_from_marcxml(_LCCN_XML)
    assert result == "PS3558.E63 D8 1984"


def test_parse_sru_response():
    result = _parse_lcc_from_marcxml(_SRU_XML)
    assert result == "PS3558.E63 D8"


def test_parse_returns_none_when_no_050():
    result = _parse_lcc_from_marcxml(_NO_050_XML)
    assert result is None


def test_parse_subfield_a_only():
    result = _parse_lcc_from_marcxml(_SUBFIELD_A_ONLY_XML)
    assert result == "PS3558.E63"


def test_parse_returns_none_on_bad_xml():
    result = _parse_lcc_from_marcxml("not xml at all {{{{")
    assert result is None


# ---------------------------------------------------------------------------
# _try_lcc_by_lccn
# ---------------------------------------------------------------------------

def test_try_lcc_by_lccn_returns_code_on_success():
    mock_resp = MagicMock(status_code=200, text=_LCCN_XML)
    with patch("compendium.services.metadata.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.get.return_value = mock_resp
        result = _try_lcc_by_lccn("65012174")
    assert result == "PS3558.E63 D8 1984"


def test_try_lcc_by_lccn_returns_none_on_404():
    mock_resp = MagicMock(status_code=404, text="")
    with patch("compendium.services.metadata.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.get.return_value = mock_resp
        result = _try_lcc_by_lccn("badlccn")
    assert result is None


def test_try_lcc_by_lccn_returns_none_on_network_error():
    with patch("compendium.services.metadata.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.get.side_effect = Exception("timeout")
        result = _try_lcc_by_lccn("65012174")
    assert result is None


# ---------------------------------------------------------------------------
# _try_lcc_by_isbn
# ---------------------------------------------------------------------------

def test_try_lcc_by_isbn_returns_code_on_success():
    mock_resp = MagicMock(status_code=200, text=_SRU_XML)
    with patch("compendium.services.metadata.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.get.return_value = mock_resp
        result = _try_lcc_by_isbn("9780441013593")
    assert result == "PS3558.E63 D8"


def test_try_lcc_by_isbn_returns_none_on_failure():
    with patch("compendium.services.metadata.httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.get.side_effect = Exception("timeout")
        result = _try_lcc_by_isbn("9780441013593")
    assert result is None


# ---------------------------------------------------------------------------
# lookup_lcc_from_loc
# ---------------------------------------------------------------------------

def test_lookup_uses_lccn_first_when_available():
    with patch("compendium.services.metadata._try_lcc_by_lccn", return_value="PS3558.E63 D8") as mock_lccn, \
         patch("compendium.services.metadata._try_lcc_by_isbn") as mock_isbn:
        result = lookup_lcc_from_loc("9780441013593", lccn="65012174")
    assert result == "PS3558.E63 D8"
    mock_lccn.assert_called_once_with("65012174")
    mock_isbn.assert_not_called()


def test_lookup_falls_back_to_isbn_when_lccn_fails():
    with patch("compendium.services.metadata._try_lcc_by_lccn", return_value=None), \
         patch("compendium.services.metadata._try_lcc_by_isbn", return_value="PS3558.E63") as mock_isbn:
        result = lookup_lcc_from_loc("9780441013593", lccn="65012174")
    assert result == "PS3558.E63"
    mock_isbn.assert_called_once_with("9780441013593")


def test_lookup_uses_isbn_when_no_lccn():
    with patch("compendium.services.metadata._try_lcc_by_lccn") as mock_lccn, \
         patch("compendium.services.metadata._try_lcc_by_isbn", return_value="PS3558.E63") as mock_isbn:
        result = lookup_lcc_from_loc("9780441013593", lccn=None)
    assert result == "PS3558.E63"
    mock_lccn.assert_not_called()
    mock_isbn.assert_called_once_with("9780441013593")


def test_lookup_returns_none_when_all_fail():
    with patch("compendium.services.metadata._try_lcc_by_lccn", return_value=None), \
         patch("compendium.services.metadata._try_lcc_by_isbn", return_value=None):
        result = lookup_lcc_from_loc("9780441013593", lccn="65012174")
    assert result is None


# ---------------------------------------------------------------------------
# parse_open_library — LCCN extraction
# ---------------------------------------------------------------------------

def test_parse_open_library_extracts_lccn():
    data = {
        "title": "Dune",
        "authors": [],
        "publishers": [],
        "cover": {},
        "identifiers": {"lccn": ["65012174"]},
    }
    meta = parse_open_library(data, "9780441013593")
    assert meta["lccn"] == "65012174"


def test_parse_open_library_lccn_none_when_missing():
    data = {"title": "Dune", "authors": [], "publishers": [], "cover": {}, "identifiers": {}}
    meta = parse_open_library(data, "9780441013593")
    assert meta["lccn"] is None
