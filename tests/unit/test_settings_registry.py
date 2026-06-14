"""Unit tests for the settings registry — type coercion, validation, and
descriptor lookup. No DB involved.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Literal

import pytest

from compendium.services.settings_registry import (
    SettingDescriptor,
    SettingValidationError,
    SettingsRegistryError,
    UnknownSettingError,
    _REGISTRY,
    encode_for_storage,
    get_descriptor,
    parse,
    register,
)


@pytest.fixture
def isolated_registry():
    """Snapshot + restore the registry so each test can register freely."""
    snapshot = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


class TestDescriptorLookup:
    def test_builtin_library_name_registered(self):
        desc = get_descriptor("library_name")
        assert desc.default == "Compendium"
        assert desc.type is str
        assert desc.scope == "librarian"

    def test_unknown_key_raises(self):
        with pytest.raises(UnknownSettingError):
            get_descriptor("not_a_real_key")

    def test_resolved_env_var_default(self):
        desc = get_descriptor("library_name")
        assert desc.resolved_env_var() == "COMPENDIUM_LIBRARY_NAME"

    def test_resolved_env_var_override(self, isolated_registry):
        desc = register(
            SettingDescriptor(
                key="custom_key",
                type=str,
                default="x",
                scope="system",
                help_text="test",
                env_var="OVERRIDE_NAME",
            )
        )
        assert desc.resolved_env_var() == "OVERRIDE_NAME"

    def test_env_value_returns_none_when_unset(self, monkeypatch):
        desc = get_descriptor("tmdb_api_key")
        monkeypatch.delenv("COMPENDIUM_TMDB_API_KEY", raising=False)
        assert desc.env_value() is None
        assert desc.env_overridden() is False

    def test_env_value_returns_none_for_empty_string(self, monkeypatch):
        desc = get_descriptor("tmdb_api_key")
        monkeypatch.setenv("COMPENDIUM_TMDB_API_KEY", "")
        assert desc.env_value() is None
        assert desc.env_overridden() is False

    def test_env_value_returns_value_when_set(self, monkeypatch):
        desc = get_descriptor("tmdb_api_key")
        monkeypatch.setenv("COMPENDIUM_TMDB_API_KEY", "abc123")
        assert desc.env_value() == "abc123"
        assert desc.env_overridden() is True

    def test_duplicate_registration_raises(self, isolated_registry):
        register(
            SettingDescriptor(
                key="dupe_key",
                type=str,
                default="a",
                scope="system",
                help_text="test",
            )
        )
        with pytest.raises(SettingsRegistryError):
            register(
                SettingDescriptor(
                    key="dupe_key",
                    type=str,
                    default="b",
                    scope="system",
                    help_text="dupe",
                )
            )


class TestBarcodeIdentifierDescriptors:
    def test_barcode_format_registered(self):
        desc = get_descriptor("barcode_format")
        assert desc.default == "10-digit"
        assert desc.scope == "librarian"
        assert desc.type == Literal["10-digit", "14-digit"]

    def test_barcode_length_not_registered(self):
        with pytest.raises(UnknownSettingError):
            get_descriptor("barcode_length")

    def test_barcode_location_enabled_not_registered(self):
        with pytest.raises(UnknownSettingError):
            get_descriptor("barcode_location_enabled")

    def test_barcode_symbology_default_is_code128(self):
        desc = get_descriptor("barcode_symbology")
        assert desc.default == "code128"
        assert desc.type == Literal["codabar", "code39", "code128"]


class TestCoercion:
    def test_str_passthrough(self):
        desc = get_descriptor("library_name")
        assert parse(desc, "Springfield Public") == "Springfield Public"

    def test_bool_truthy_values(self):
        desc = get_descriptor("guest_search_enabled")
        for raw in ("true", "True", "1", "yes", "on"):
            assert parse(desc, raw) is True

    def test_bool_falsy_values(self):
        desc = get_descriptor("guest_search_enabled")
        for raw in ("false", "False", "0", "no", "off"):
            assert parse(desc, raw) is False

    def test_bool_invalid_raises(self):
        desc = get_descriptor("guest_search_enabled")
        with pytest.raises(SettingValidationError):
            parse(desc, "maybe")

    def test_literal_valid(self):
        desc = get_descriptor("default_theme")
        assert parse(desc, "dark") == "dark"
        assert parse(desc, "auto") == "auto"

    def test_literal_invalid_raises(self):
        desc = get_descriptor("default_theme")
        with pytest.raises(SettingValidationError):
            parse(desc, "neon")

    def test_int_coerces(self, isolated_registry):
        desc = register(
            SettingDescriptor(
                key="int_key", type=int, default=0, scope="system", help_text="x"
            )
        )
        assert parse(desc, "42") == 42

    def test_int_invalid_raises(self, isolated_registry):
        desc = register(
            SettingDescriptor(
                key="int_key", type=int, default=0, scope="system", help_text="x"
            )
        )
        with pytest.raises(SettingValidationError):
            parse(desc, "abc")

    def test_list_comma_separated(self, isolated_registry):
        desc = register(
            SettingDescriptor(
                key="list_key",
                type=list[str],
                default=[],
                scope="system",
                help_text="x",
            )
        )
        assert parse(desc, "a,b,c") == ["a", "b", "c"]

    def test_list_json(self, isolated_registry):
        desc = register(
            SettingDescriptor(
                key="list_json_key",
                type=list[int],
                default=[],
                scope="system",
                help_text="x",
            )
        )
        assert parse(desc, "[1, 2, 3]") == [1, 2, 3]

    def test_list_empty_string(self, isolated_registry):
        desc = register(
            SettingDescriptor(
                key="list_empty_key",
                type=list[str],
                default=[],
                scope="system",
                help_text="x",
            )
        )
        assert parse(desc, "") == []


class TestValidator:
    def test_validator_failure_raises(self, isolated_registry):
        def positive(v: int) -> None:
            if v <= 0:
                raise ValueError("must be positive")

        desc = register(
            SettingDescriptor(
                key="positive_int",
                type=int,
                default=1,
                scope="system",
                help_text="x",
                validator=positive,
            )
        )
        with pytest.raises(SettingValidationError):
            parse(desc, "-1")
        # Valid still works
        assert parse(desc, "5") == 5


class TestEncoding:
    def test_str_roundtrip(self):
        desc = get_descriptor("library_name")
        raw = encode_for_storage("Springfield", desc.type)
        assert parse(desc, raw) == "Springfield"

    def test_bool_roundtrip(self):
        desc = get_descriptor("guest_search_enabled")
        assert encode_for_storage(True, desc.type) == "true"
        assert encode_for_storage(False, desc.type) == "false"
        assert parse(desc, encode_for_storage(True, desc.type)) is True
        assert parse(desc, encode_for_storage(False, desc.type)) is False

    def test_literal_roundtrip(self):
        desc = get_descriptor("default_theme")
        raw = encode_for_storage("dark", desc.type)
        assert parse(desc, raw) == "dark"

    def test_list_roundtrip(self, isolated_registry):
        desc = register(
            SettingDescriptor(
                key="list_rt",
                type=list[str],
                default=[],
                scope="system",
                help_text="x",
            )
        )
        raw = encode_for_storage(["a", "b"], desc.type)
        assert parse(desc, raw) == ["a", "b"]


class TestShortcutValidator:
    """Tests for _shortcut_list validator via custom_shortcuts descriptor."""

    @pytest.fixture(autouse=True)
    def desc(self):
        from compendium.services.settings_registry import get_descriptor, validate

        self._desc = get_descriptor("custom_shortcuts")
        self._validate = validate

    def _run(self, value):
        self._validate(self._desc, value)

    def test_empty_list_is_valid(self):
        self._run([])

    def test_valid_relative_url(self):
        self._run(["/ui/admin/holds"])

    def test_https_url_rejected(self):
        with pytest.raises(Exception):
            self._run(["https://helpdesk.example.com"])

    def test_http_url_rejected(self):
        with pytest.raises(Exception):
            self._run(["http://intranet/library"])

    def test_up_to_five_entries(self):
        self._run([f"/ui/path{i}" for i in range(5)])

    def test_over_five_entries_rejected(self):
        with pytest.raises(Exception):
            self._run([f"/ui/path{i}" for i in range(6)])

    def test_empty_string_rejected(self):
        with pytest.raises(Exception):
            self._run([""])

    def test_javascript_scheme_rejected(self):
        with pytest.raises(Exception):
            self._run(["javascript:alert(1)"])

    def test_non_list_rejected(self):
        with pytest.raises(Exception):
            self._run("/ui/holds")

    def test_non_string_entry_rejected(self):
        with pytest.raises(Exception):
            self._run([123])


class TestCustomShortcutsJinjaGlobal:
    # jinja.py binds get_site_setting at import time — patch there, not at origin.
    # These cover label-resolution / trimming / unknown-url skipping; an admin
    # (wildcard) user is passed so permission-filtering doesn't drop the staff
    # pages under test. Permission-filtering itself is covered in
    # tests/unit/test_nav_shortcuts.py.

    _ADMIN = SimpleNamespace(role=SimpleNamespace(permissions=["*"]))

    def test_resolves_labels_from_nav_pages(self, monkeypatch):
        import compendium.web.jinja as jinja_mod

        monkeypatch.setattr(
            jinja_mod, "get_site_setting", lambda key: ["/ui/admin/holds", "/ui/items/new"]
        )
        result = jinja_mod._jinja_custom_shortcuts(self._ADMIN)
        assert result == [
            {"label": "Holds Queue", "url": "/ui/admin/holds"},
            {"label": "Add Item", "url": "/ui/items/new"},
        ]

    def test_empty_setting_returns_empty(self, monkeypatch):
        import compendium.web.jinja as jinja_mod

        monkeypatch.setattr(jinja_mod, "get_site_setting", lambda key: [])
        assert jinja_mod._jinja_custom_shortcuts(self._ADMIN) == []

    def test_trims_whitespace(self, monkeypatch):
        import compendium.web.jinja as jinja_mod

        monkeypatch.setattr(jinja_mod, "get_site_setting", lambda key: ["  /ui/admin/holds  "])
        result = jinja_mod._jinja_custom_shortcuts(self._ADMIN)
        assert result == [{"label": "Holds Queue", "url": "/ui/admin/holds"}]

    def test_skips_unknown_urls(self, monkeypatch):
        import compendium.web.jinja as jinja_mod

        monkeypatch.setattr(
            jinja_mod, "get_site_setting", lambda key: ["/ui/admin/holds", "/ui/no-longer-exists"]
        )
        result = jinja_mod._jinja_custom_shortcuts(self._ADMIN)
        assert result == [{"label": "Holds Queue", "url": "/ui/admin/holds"}]


class TestNavPages:
    def test_all_pages_have_required_keys(self):
        from compendium.web.nav_pages import NAV_PAGES

        for p in NAV_PAGES:
            assert "key" in p and "label" in p and "url" in p and "permission" in p, p

    def test_all_pages_have_section(self):
        from compendium.web.nav_pages import NAV_PAGES

        valid = {"Catalog", "Circulation", "Cataloging", "Admin", "Settings", "Self-service"}
        for p in NAV_PAGES:
            assert p.get("section") in valid, p

    def test_all_urls_are_relative(self):
        from compendium.web.nav_pages import NAV_PAGES

        for p in NAV_PAGES:
            assert p["url"].startswith("/"), f"{p['key']}: {p['url']}"

    def test_all_keys_are_unique(self):
        from compendium.web.nav_pages import NAV_PAGES

        keys = [p["key"] for p in NAV_PAGES]
        assert len(keys) == len(set(keys))

    def test_all_urls_are_unique(self):
        from compendium.web.nav_pages import NAV_PAGES

        urls = [p["url"] for p in NAV_PAGES]
        assert len(urls) == len(set(urls))


class TestShortcutPageGlobals:
    def test_shortcut_pages_returns_all(self):
        from compendium.web.jinja import _jinja_shortcut_pages
        from compendium.web.nav_pages import NAV_PAGES

        result = _jinja_shortcut_pages()
        assert result is NAV_PAGES

    def test_shortcut_pages_for_user_filters_by_permission(self):
        from unittest.mock import MagicMock
        from compendium.web.jinja import _jinja_shortcut_pages_for_user

        user = MagicMock()
        user.role.permissions = ["loan.checkout"]
        result = _jinja_shortcut_pages_for_user(user)
        urls = [p["url"] for p in result]
        assert "/ui/circ" in urls
        assert "/ui/kiosk" in urls
        assert "/ui/admin/loans" not in urls

    def test_shortcut_pages_for_user_includes_no_permission_pages(self):
        from unittest.mock import MagicMock
        from compendium.web.jinja import _jinja_shortcut_pages_for_user

        user = MagicMock()
        user.role.permissions = []
        result = _jinja_shortcut_pages_for_user(user)
        urls = [p["url"] for p in result]
        assert "/ui/catalog" in urls
        assert "/ui/me/loans" in urls

    def test_shortcut_pages_for_none_user_returns_empty(self):
        from compendium.web.jinja import _jinja_shortcut_pages_for_user

        assert _jinja_shortcut_pages_for_user(None) == []


def test_circulation_scan_isbn_enabled_descriptor():
    d = get_descriptor("circulation_scan_isbn_enabled")
    assert d.type is bool
    assert d.default is True
    assert d.scope == "librarian"
    assert d.resolved_env_var() == "COMPENDIUM_CIRCULATION_SCAN_ISBN_ENABLED"
