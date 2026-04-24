"""Registry of site-settings descriptors.

The registry is the canonical source for *what settings exist*. The
``site_setting`` table holds *overrides*; env vars also override at read
time (break-glass). A setting must be registered here before any caller
can read or write it via ``get_site_setting`` / ``set_site_setting``.

Each descriptor declares its type, default, scope (``librarian`` vs
``system`` — consumed by slice B/C UI), help text, and optional validator.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, get_args, get_origin


Scope = Literal["librarian", "system"]


class SettingsRegistryError(Exception):
    pass


class UnknownSettingError(SettingsRegistryError, KeyError):
    """Raised when a key isn't registered."""


class SettingValidationError(SettingsRegistryError, ValueError):
    """Raised when a value fails type coercion or a validator."""


@dataclass(frozen=True)
class SettingDescriptor:
    key: str
    type: type | object  # may be a typing alias e.g. list[str]
    default: Any
    scope: Scope
    help_text: str
    validator: Callable[[Any], None] | None = None
    env_var: str | None = None

    def resolved_env_var(self) -> str:
        return self.env_var or f"COMPENDIUM_{self.key.upper()}"


_REGISTRY: dict[str, SettingDescriptor] = {}


def register(desc: SettingDescriptor) -> SettingDescriptor:
    if desc.key in _REGISTRY:
        raise SettingsRegistryError(f"setting already registered: {desc.key}")
    _REGISTRY[desc.key] = desc
    return desc


def get_descriptor(key: str) -> SettingDescriptor:
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise UnknownSettingError(key) from exc


def all_descriptors() -> list[SettingDescriptor]:
    return list(_REGISTRY.values())


def _is_list_type(t: Any) -> bool:
    return get_origin(t) is list


def _coerce(value: str, t: Any) -> Any:
    """Parse a string (from env or DB) into the descriptor's target type.

    Raises SettingValidationError on mismatch. This is intentionally strict —
    we prefer a loud failure to silently returning a bad default at read time.
    """
    if t is str:
        return value
    if t is bool:
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise SettingValidationError(f"cannot parse {value!r} as bool")
    if t is int:
        try:
            return int(value)
        except ValueError as exc:
            raise SettingValidationError(f"cannot parse {value!r} as int") from exc
    if t is float:
        try:
            return float(value)
        except ValueError as exc:
            raise SettingValidationError(f"cannot parse {value!r} as float") from exc
    if _is_list_type(t):
        inner = get_args(t)[0] if get_args(t) else str
        if not value:
            return []
        # JSON array form or comma-separated — detect cheaply
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise SettingValidationError(
                    f"cannot parse {value!r} as JSON list"
                ) from exc
            if not isinstance(parsed, list):
                raise SettingValidationError(f"expected JSON list, got {type(parsed)}")
            return [_coerce(str(v), inner) for v in parsed]
        return [_coerce(part.strip(), inner) for part in value.split(",") if part.strip()]
    if get_origin(t) is Literal:
        allowed = get_args(t)
        if value in allowed:
            return value
        raise SettingValidationError(
            f"{value!r} not in allowed literals {allowed!r}"
        )
    raise SettingsRegistryError(f"unsupported descriptor type: {t!r}")


def encode_for_storage(value: Any, t: Any) -> str:
    """Serialize a Python value to the text form stored in the DB / env."""
    if t is str:
        return str(value)
    if t is bool:
        return "true" if value else "false"
    if t in (int, float):
        return str(value)
    if _is_list_type(t):
        return json.dumps(value)
    if get_origin(t) is Literal:
        return str(value)
    raise SettingsRegistryError(f"unsupported descriptor type: {t!r}")


def validate(desc: SettingDescriptor, value: Any) -> None:
    """Run the descriptor's validator, raising SettingValidationError on fail."""
    if desc.validator is None:
        return
    try:
        desc.validator(value)
    except (ValueError, TypeError) as exc:
        raise SettingValidationError(str(exc)) from exc


def parse(desc: SettingDescriptor, raw: str) -> Any:
    """Coerce a raw string (DB text or env var) into the typed value."""
    value = _coerce(raw, desc.type)
    validate(desc, value)
    return value


# ---------------------------------------------------------------------------
# Built-in descriptors
#
# Slice A registers only a handful of settings to shake down the pattern
# across multiple types (str, bool, Literal). Slice C migrates the rest.
# ---------------------------------------------------------------------------


def _register_builtins() -> None:
    register(
        SettingDescriptor(
            key="library_name",
            type=str,
            default="Compendium",
            scope="librarian",
            help_text=(
                "Name of the library. Appears in the nav brand, email "
                "templates, and printed patron cards."
            ),
        )
    )
    register(
        SettingDescriptor(
            key="default_theme",
            type=Literal["light", "dark", "auto"],
            default="light",
            scope="librarian",
            help_text=(
                "Server-rendered default theme for visitors who haven't "
                "picked one. 'auto' follows prefers-color-scheme."
            ),
        )
    )
    register(
        SettingDescriptor(
            key="guest_search_enabled",
            type=bool,
            default=True,
            scope="librarian",
            help_text=(
                "When enabled, unauthenticated visitors can search the "
                "catalog. When disabled, all search endpoints require login."
            ),
        )
    )
    register(
        SettingDescriptor(
            key="currency_symbol",
            type=str,
            default="$",
            scope="librarian",
            help_text="Symbol prefixed or suffixed to fine amounts.",
        )
    )


_register_builtins()
