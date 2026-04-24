"""Unit tests for the settings registry — type coercion, validation, and
descriptor lookup. No DB involved.
"""
from __future__ import annotations

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
