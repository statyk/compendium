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
    display_name: str = ""  # Human-friendly label; falls back to key.title() if blank.
    validator: Callable[[Any], None] | None = None
    env_var: str | None = None
    nullable: bool = False  # If True, an empty string parses to None.

    def resolved_env_var(self) -> str:
        return self.env_var or f"COMPENDIUM_{self.key.upper()}"

    def resolved_display_name(self) -> str:
        return self.display_name or self.key.replace("_", " ").title()


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


def env_only_field_names() -> list[str]:
    """Pydantic ``Settings`` field names that are NOT covered by the registry.

    These are the items that only ever live in environment variables (DB URL,
    JWT secret, TLS material, the SMTP password, etc.). Useful for tooling
    that wants the union of "every COMPENDIUM_* env var the app recognizes."
    """
    from compendium.config.settings import Settings

    registered = set(_REGISTRY.keys())
    return sorted(
        name for name in Settings.model_fields if name not in registered
    )


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
    if value is None:
        return ""
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
    if value is None and desc.nullable:
        return
    if desc.validator is None:
        return
    try:
        desc.validator(value)
    except (ValueError, TypeError) as exc:
        raise SettingValidationError(str(exc)) from exc


def parse(desc: SettingDescriptor, raw: str) -> Any:
    """Coerce a raw string (DB text or env var) into the typed value."""
    if desc.nullable and raw == "":
        return None
    value = _coerce(raw, desc.type)
    validate(desc, value)
    return value


# ---------------------------------------------------------------------------
# Built-in descriptors
#
# Slice A registers only a handful of settings to shake down the pattern
# across multiple types (str, bool, Literal). Slice C migrates the rest.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Common validators
# ---------------------------------------------------------------------------


def _positive_int(v: int) -> None:
    if not isinstance(v, int) or v <= 0:
        raise ValueError("must be a positive integer")


def _non_negative_int(v: int) -> None:
    if not isinstance(v, int) or v < 0:
        raise ValueError("must be a non-negative integer")


def _port_range(v: int) -> None:
    if not isinstance(v, int) or not (1 <= v <= 65535):
        raise ValueError("must be a TCP port (1-65535)")


def _all_positive_ints(v: list) -> None:
    if not isinstance(v, list):
        raise ValueError("must be a list")
    for x in v:
        if not isinstance(x, int) or x <= 0:
            raise ValueError(f"each entry must be a positive int, got {x!r}")


def _register_builtins() -> None:
    # ── Librarian-tier ─────────────────────────────────────────────────────
    register(
        SettingDescriptor(
            key="library_name",
            display_name="Library Name",
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
            display_name="Default Theme",
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
            display_name="Enable Guest Search",
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
            display_name="Currency Symbol",
            type=str,
            default="$",
            scope="librarian",
            help_text="Symbol prefixed or suffixed to fine amounts.",
        )
    )
    register(
        SettingDescriptor(
            key="currency_symbol_position",
            display_name="Currency Symbol Position",
            type=Literal["before", "after"],
            default="before",
            scope="librarian",
            help_text="Whether the currency symbol appears before or after the amount.",
        )
    )
    register(
        SettingDescriptor(
            key="fine_block_threshold_cents",
            display_name="Fine Block Threshold (cents)",
            type=int,
            default=None,
            nullable=True,
            scope="librarian",
            help_text=(
                "Outstanding-fine threshold (in cents) at which patrons "
                "are blocked from new checkouts. Leave empty to disable."
            ),
            validator=_non_negative_int,
        )
    )
    register(
        SettingDescriptor(
            key="fine_block_holds",
            display_name="Block Holds When Over Fine Threshold",
            type=bool,
            default=False,
            scope="librarian",
            help_text=(
                "When enabled, the fine-block threshold also blocks placing "
                "new holds. Default is to block checkouts only."
            ),
        )
    )
    register(
        SettingDescriptor(
            key="overdue_tiers",
            display_name="Overdue Notification Tiers (days)",
            type=list[int],
            default=[3, 14, 30],
            scope="librarian",
            help_text=(
                "Days-overdue checkpoints that trigger a notification. "
                "One notice per highest matching tier per loan."
            ),
            validator=_all_positive_ints,
        )
    )
    register(
        SettingDescriptor(
            key="due_soon_days_before",
            display_name="Due-Soon Reminder Lead Time (days)",
            type=int,
            default=3,
            scope="librarian",
            help_text="How many days ahead of due date to send the due-soon reminder.",
            validator=_positive_int,
        )
    )
    register(
        SettingDescriptor(
            key="kiosk_idle_timeout_seconds",
            display_name="Kiosk Idle Timeout (seconds)",
            type=int,
            default=60,
            scope="librarian",
            help_text=(
                "How long a self-checkout session waits idle before "
                "redirecting back to the landing page."
            ),
            validator=_positive_int,
        )
    )
    register(
        SettingDescriptor(
            key="default_loan_period_days",
            display_name="Default Loan Period (days)",
            type=int,
            default=14,
            scope="librarian",
            help_text=(
                "Loan period applied when no policy matches the item's "
                "media type / patron category."
            ),
            validator=_positive_int,
        )
    )
    register(
        SettingDescriptor(
            key="hold_expiry_days",
            display_name="Hold Queue Expiry (days)",
            type=int,
            default=30,
            scope="librarian",
            help_text="How many days a WAITING hold sits before auto-cancelling.",
            validator=_positive_int,
        )
    )
    register(
        SettingDescriptor(
            key="hold_pickup_days",
            display_name="Pickup Shelf Window (days)",
            type=int,
            default=3,
            scope="librarian",
            help_text=(
                "How many days a notified hold (AVAILABLE on the pickup "
                "shelf) sits before auto-cancelling."
            ),
            validator=_positive_int,
        )
    )

    # ── System-tier (infrastructure) ───────────────────────────────────────
    register(
        SettingDescriptor(
            key="smtp_host",
            display_name="SMTP Host",
            type=str,
            default=None,
            nullable=True,
            scope="system",
            help_text=(
                "SMTP server hostname. Leave empty to disable email "
                "delivery (notifications still queue but stay 'skipped')."
            ),
        )
    )
    register(
        SettingDescriptor(
            key="smtp_port",
            display_name="SMTP Port",
            type=int,
            default=587,
            scope="system",
            help_text="SMTP TCP port.",
            validator=_port_range,
        )
    )
    register(
        SettingDescriptor(
            key="smtp_username",
            display_name="SMTP Username",
            type=str,
            default=None,
            nullable=True,
            scope="system",
            help_text="Username for SMTP auth, if required.",
        )
    )
    register(
        SettingDescriptor(
            key="smtp_use_starttls",
            display_name="Use STARTTLS",
            type=bool,
            default=True,
            scope="system",
            help_text="Issue STARTTLS after the initial connection (port 587).",
        )
    )
    register(
        SettingDescriptor(
            key="smtp_use_ssl",
            display_name="Use Implicit TLS (SMTPS)",
            type=bool,
            default=False,
            scope="system",
            help_text=(
                "Use SMTPS (implicit TLS, typically port 465). Mutually "
                "exclusive with STARTTLS."
            ),
        )
    )
    register(
        SettingDescriptor(
            key="smtp_from_address",
            display_name="From Address",
            type=str,
            default=None,
            nullable=True,
            scope="system",
            help_text="Email address used in the From header. Leave empty to disable delivery.",
        )
    )
    register(
        SettingDescriptor(
            key="smtp_from_name",
            display_name="From Display Name",
            type=str,
            default="Compendium",
            scope="system",
            help_text="Display name used in the From header.",
        )
    )
    register(
        SettingDescriptor(
            key="notifications_batch_size",
            display_name="Notifications Batch Size",
            type=int,
            default=50,
            scope="system",
            help_text="Max notifications drained per cron-invoked send.",
            validator=_positive_int,
        )
    )
    register(
        SettingDescriptor(
            key="notifications_max_attempts",
            display_name="Notifications Max Attempts",
            type=int,
            default=5,
            scope="system",
            help_text="Max delivery attempts before a notification is marked 'failed'.",
            validator=_positive_int,
        )
    )
    register(
        SettingDescriptor(
            key="notification_retention_days",
            display_name="Notification Retention (days)",
            type=int,
            default=None,
            nullable=True,
            scope="system",
            help_text=(
                "Auto-prune sent + cancelled notifications older than this "
                "many days. Leave empty to keep forever."
            ),
            validator=_non_negative_int,
        )
    )
    register(
        SettingDescriptor(
            key="audit_retention_days",
            display_name="Audit Log Retention (days)",
            type=int,
            default=None,
            nullable=True,
            scope="system",
            help_text=(
                "Default --older-than-days for the audit-log prune "
                "maintenance command. Leave empty for no default."
            ),
            validator=_non_negative_int,
        )
    )
    register(
        SettingDescriptor(
            key="login_max_failures",
            display_name="Login Max Failures",
            type=int,
            default=10,
            scope="system",
            help_text=(
                "Number of consecutive failed login attempts (per username or "
                "kiosk card number) allowed within the throttle window before "
                "further attempts are blocked. Set to 0 to disable throttling."
            ),
            validator=_non_negative_int,
        )
    )
    register(
        SettingDescriptor(
            key="login_failure_window_seconds",
            display_name="Login Failure Window (seconds)",
            type=int,
            default=300,
            scope="system",
            help_text=(
                "Sliding-window duration for counting failed login attempts. "
                "Failures older than this (in seconds) are ignored. Default "
                "300 = 5 minutes."
            ),
            validator=_positive_int,
        )
    )
    register(
        SettingDescriptor(
            key="password_min_length",
            display_name="Password Minimum Length",
            type=int,
            default=8,
            scope="librarian",
            help_text=(
                "Minimum character length for user passwords. "
                "NIST SP 800-63B recommends at least 8. "
                "Existing passwords are not retroactively affected."
            ),
            validator=_positive_int,
        )
    )
    register(
        SettingDescriptor(
            key="bcrypt_rounds",
            display_name="bcrypt Cost Factor",
            type=int,
            default=12,
            scope="system",
            help_text=(
                "bcrypt cost factor used when hashing new passwords. "
                "Higher values are slower and more resistant to brute-force. "
                "Must be between 4 and 15. Existing password hashes are "
                "not affected (bcrypt embeds the cost in each hash)."
            ),
            validator=lambda v: (
                None if 4 <= int(v) <= 15
                else "bcrypt_rounds must be between 4 and 15"
            ),
        )
    )


_register_builtins()
