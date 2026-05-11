"""Unit tests for XML entity injection guard in MARCXML import (M4/defusedxml)."""
from __future__ import annotations

import pytest

from compendium.domain.errors import ValidationError
from compendium.services.import_export import _reject_xml_entity_declarations


_VALID_MARCXML = b"""<?xml version="1.0" encoding="UTF-8"?>
<collection xmlns="http://www.loc.gov/MARC21/slim">
  <record>
    <leader>00000cam a2200000 i 4500</leader>
    <datafield tag="245" ind1="1" ind2="0">
      <subfield code="a">A Safe Title</subfield>
    </datafield>
  </record>
</collection>"""

_ENTITY_BOMB = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE bomb [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
]>
<collection xmlns="http://www.loc.gov/MARC21/slim"/>"""

_ENTITY_INLINE = b"""<?xml version="1.0" encoding="UTF-8"?>
<collection xmlns="http://www.loc.gov/MARC21/slim">
  <!ENTITY evil "evil">
</collection>"""


class TestRejectXmlEntityDeclarations:
    def test_entity_in_doctype_rejected(self):
        with pytest.raises(ValidationError, match="entity declarations"):
            _reject_xml_entity_declarations(_ENTITY_BOMB)

    def test_entity_inline_rejected(self):
        with pytest.raises(ValidationError, match="entity declarations"):
            _reject_xml_entity_declarations(_ENTITY_INLINE)

    def test_case_insensitive_match(self):
        payload = b"  <!ENTITY lol 'lol'>"
        with pytest.raises(ValidationError, match="entity declarations"):
            _reject_xml_entity_declarations(payload)

    def test_mixed_case(self):
        payload = b"<!EnTiTy x 'y'>"
        with pytest.raises(ValidationError, match="entity declarations"):
            _reject_xml_entity_declarations(payload)

    def test_valid_marcxml_not_rejected(self):
        _reject_xml_entity_declarations(_VALID_MARCXML)  # must not raise

    def test_empty_bytes_not_rejected(self):
        _reject_xml_entity_declarations(b"")
