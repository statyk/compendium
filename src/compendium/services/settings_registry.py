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
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, get_args, get_origin


Scope = Literal["librarian", "system"]


class SettingsRegistryError(Exception):
    pass


@dataclass
class AvailabilityHint:
    """Signals that certain choices for a setting are unavailable.

    Used by the admin UI to grey out options and display a contextual warning.
    """
    unavailable_choices: frozenset
    warning: str


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
    short_help: str = ""  # One-line summary shown inline; full help_text moves to a tooltip.
    display_name: str = ""  # Human-friendly label; falls back to key.title() if blank.
    validator: Callable[[Any], None] | None = None
    env_var: str | None = None
    nullable: bool = False  # If True, an empty string parses to None.
    widget: str | None = None  # Hint for custom UI rendering (e.g. "shortcut_picker").
    secret: bool = False  # If True, value is encrypted at rest; never echoed in UI.
    availability_hint: Callable[[], "AvailabilityHint | None"] | None = field(default=None)

    def resolved_env_var(self) -> str:
        return self.env_var or f"COMPENDIUM_{self.key.upper()}"

    def env_value(self) -> str | None:
        """The env override for this setting, or None if unset OR empty.

        Empty-string is treated as unset to match docker-compose's ``${VAR:-}``
        pattern (which sets the var to "" when the host-side value is absent).
        This mirrors the Settings model_validator that strips empty strings
        before pydantic parses them.
        """
        raw = os.environ.get(self.resolved_env_var())
        return raw if raw else None

    def env_overridden(self) -> bool:
        return self.env_value() is not None

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


def _one_of(*choices: str):
    """Return a validator that checks the value is one of the given strings."""
    def _validate(v: str) -> None:
        if v not in choices:
            raise ValueError(f"must be one of: {', '.join(choices)}")
    return _validate


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


def _four_digit_code(v: str) -> None:
    if not isinstance(v, str) or len(v) != 4 or not v.isdigit():
        raise ValueError("must be exactly 4 decimal digits (e.g. 0000, 0001)")


def _shortcut_list(v: list) -> None:
    if not isinstance(v, list):
        raise ValueError("must be a list")
    if len(v) > 5:
        raise ValueError("at most 5 shortcuts allowed")
    for entry in v:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"each entry must be a non-empty URL string, got {entry!r}")
        if not entry.strip().startswith("/"):
            raise ValueError(
                f"URL must be a relative internal path starting with / — got {entry.strip()!r}"
            )


def _validate_timezone(value: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, KeyError):
        raise SettingValidationError(f"'{value}' is not a valid IANA timezone name.")


def _register_builtins() -> None:
    # ── Librarian-tier ─────────────────────────────────────────────────────
    register(
        SettingDescriptor(
            key="library_name",
            display_name="Library Name",
            type=str,
            default="Compendium",
            scope="librarian",
            short_help="Name of the library, shown in the nav, emails, and cards.",
            help_text=(
                "Name of the library. Appears in the nav brand, email "
                "templates, and printed patron cards."
            ),
        )
    )
    register(
        SettingDescriptor(
            key="library_timezone",
            display_name="Library Timezone",
            type=str,
            default="UTC",
            scope="librarian",
            short_help="IANA timezone used to compute due dates and closed days.",
            help_text=(
                "IANA timezone name for the library (e.g. 'America/New_York', "
                "'America/Chicago', 'America/Los_Angeles'). Used to compute "
                "due-date rolling and closed-day deductions relative to local "
                "calendar days. The env var COMPENDIUM_LIBRARY_TIMEZONE overrides "
                "the DB value."
            ),
            validator=_validate_timezone,
            widget="timezone_picker",
        )
    )
    register(
        SettingDescriptor(
            key="default_theme",
            display_name="Default Theme",
            type=Literal["light", "dark", "auto"],
            default="light",
            scope="librarian",
            short_help="Default theme for visitors who haven't picked one.",
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
            short_help="Let unauthenticated visitors search the catalog.",
            help_text=(
                "When enabled, unauthenticated visitors can search the "
                "catalog. When disabled, all search endpoints require login."
            ),
        )
    )
    register(
        SettingDescriptor(
            key="trash_retention_days",
            display_name="Trash Retention (days)",
            type=int,
            default=90,
            scope="librarian",
            short_help="Days a deleted work stays restorable before purge.",
            help_text=(
                "How long deleted works stay in the trash before "
                "'compendium maintenance purge-trash' removes them permanently. "
                "0 disables time-based purging (manual purge only). The env var "
                "COMPENDIUM_TRASH_RETENTION_DAYS overrides the DB value."
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
            short_help="Outstanding-fine level (cents) that blocks new checkouts.",
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
            short_help="Also block placing holds when over the fine threshold.",
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
            short_help="Days-overdue checkpoints that trigger a notification.",
            help_text=(
                "Days-overdue checkpoints that trigger a notification. "
                "One notice per highest matching tier per loan."
            ),
            validator=_all_positive_ints,
        )
    )
    register(
        SettingDescriptor(
            key="custom_shortcuts",
            display_name="Nav Shortcuts",
            type=list[str],
            # Empty by default — a fresh install shouldn't preload staff-only
            # quick links. Each library opts in via the settings picker, and
            # users can override locally via the nav pencil icon.
            default=[],
            scope="librarian",
            short_help="Up to 5 quick-access nav links for logged-in users.",
            help_text=(
                "Up to 5 quick-access links shown in the nav bar for logged-in "
                "users. Select internal pages using the picker below. "
                "Each user can also override this list locally from the pencil "
                "icon in the nav."
            ),
            validator=_shortcut_list,
            widget="shortcut_picker",
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
            short_help="Idle time before a kiosk session returns to the landing page.",
            help_text=(
                "How long a self-checkout session waits idle before "
                "redirecting back to the landing page."
            ),
            validator=_positive_int,
        )
    )
    register(
        SettingDescriptor(
            key="public_base_url",
            display_name="Public Base URL",
            type=str,
            default=None,
            nullable=True,
            scope="librarian",
            short_help="Externally reachable base URL for phone-pairing QR codes.",
            help_text=(
                "The externally reachable base URL of this server (e.g. "
                "'https://library.example.org'), used to build the QR code a "
                "phone scans to pair. Must be https — phone cameras refuse to "
                "start on a non-secure origin. Leave empty to derive it from the "
                "staff request (honoring X-Forwarded-Proto behind a reverse "
                "proxy). The env var COMPENDIUM_PUBLIC_BASE_URL overrides the DB "
                "value."
            ),
        )
    )
    register(
        SettingDescriptor(
            key="scan_session_minutes",
            display_name="Phone Scan Session Lifetime (minutes)",
            type=int,
            default=60,
            scope="librarian",
            short_help="How long a paired phone-scanner session stays valid.",
            help_text=(
                "Lifetime, in minutes, of a paired phone-scanner desk session "
                "before it expires and the phone must re-pair. Parallels the "
                "kiosk idle timeout. The env var COMPENDIUM_SCAN_SESSION_MINUTES "
                "overrides the DB value."
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
            short_help="Loan period used when no policy matches.",
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
    register(
        SettingDescriptor(
            key="circulation_scan_isbn_enabled",
            display_name="Circulate by ISBN / UPC",
            type=bool,
            default=True,
            scope="librarian",
            short_help=(
                "Allows use of an ISBN or UPC instead of library barcode "
                "for circulation lookups."
            ),
            help_text=(
                "Allows you to enter or scan an ISBN or UPC instead of the "
                "item's barcode number. For works with multiple copies, "
                "chooses one automatically where possible."
            ),
        )
    )

    # ── Identifiers & barcodes ─────────────────────────────────────────────
    register(
        SettingDescriptor(
            key="barcode_format",
            display_name="Barcode Format",
            type=Literal["10-digit", "14-digit"],
            default="10-digit",
            scope="librarian",
            short_help="Format for newly minted item barcodes and patron cards.",
            help_text=(
                "Format for newly minted item barcodes and patron card numbers. "
                "'10-digit' omits the branch location prefix; '14-digit' embeds "
                "a 4-digit branch location code before the unique slug. Existing "
                "barcodes are unaffected — both lengths remain scannable."
            ),
        )
    )
    register(
        SettingDescriptor(
            key="barcode_default_location_code",
            display_name="Default Location Code",
            type=str,
            default="0000",
            scope="librarian",
            short_help="Four-digit fallback location code for barcode minting.",
            help_text=(
                "Four-digit fallback location code used when minting barcodes for "
                "items with no assigned branch, or branches with no Location Code "
                "set. Must be exactly 4 decimal digits (e.g. 0000, 0001)."
            ),
            validator=_four_digit_code,
        )
    )
    register(
        SettingDescriptor(
            key="barcode_symbology",
            display_name="Barcode Symbology",
            type=Literal["codabar", "code39", "code128"],
            default="code128",
            scope="librarian",
            short_help="Barcode symbology for printed labels (Code 128 recommended).",
            help_text=(
                "Barcode symbology for printed labels. Code 128 is recommended — "
                "it produces shorter barcodes than Codabar or Code 39, which "
                "matters for compact spine labels. Match this setting to what "
                "your scanner is configured to read."
            ),
        )
    )

    # ── Label defaults ─────────────────────────────────────────────────────
    # Each setting stores the list of optional fields shown by default for
    # one label kind. Required fields (e.g. call_number for spine) are always
    # drawn regardless. Admins can change these here; per-call form/CLI flags
    # still override per-generation.
    def _label_fields_validator(kind_format: str):
        from compendium.services.labels import OPTIONAL_FIELDS
        allowed = OPTIONAL_FIELDS.get(kind_format, frozenset())

        def _validate(v: list) -> None:
            bad = [f for f in v if f not in allowed]
            if bad:
                raise ValueError(
                    f"unknown field(s) for {kind_format!r}: {bad!r}. "
                    f"Allowed: {sorted(allowed)}"
                )
        return _validate

    register(
        SettingDescriptor(
            key="label_spine_default_fields",
            display_name="Spine label — default fields",
            type=list[str],
            default=["call_number", "location", "cutter", "year"],
            scope="librarian",
            short_help="Fields shown by default on spine labels.",
            help_text=(
                "Fields shown by default on spine labels. "
                "Allowed: call_number, barcode, location, branch, cutter, year. "
                "Enter as a comma-separated list."
            ),
            validator=_label_fields_validator("spine"),
        )
    )
    register(
        SettingDescriptor(
            key="label_pocket_default_fields",
            display_name="Pocket label — default fields",
            type=list[str],
            default=["barcode", "title", "author", "call_number", "cutter", "year"],
            scope="librarian",
            short_help="Fields shown by default on pocket labels.",
            help_text=(
                "Fields shown by default on pocket labels. "
                "Allowed: title, author, call_number, barcode, cutter, year, branch, library_name. "
                "Enter as a comma-separated list."
            ),
            validator=_label_fields_validator("pocket"),
        )
    )
    register(
        SettingDescriptor(
            key="label_barcode_only_default_fields",
            display_name="Barcode-only label — default fields",
            type=list[str],
            default=["barcode", "human_readable"],
            scope="librarian",
            short_help="Fields shown by default on barcode-only stickers.",
            help_text=(
                "Fields shown by default on barcode-only stickers. "
                "Allowed: barcode, title, human_readable. "
                "Enter as a comma-separated list."
            ),
            validator=_label_fields_validator("barcode-only"),
        )
    )
    register(
        SettingDescriptor(
            key="label_patron_full_default_fields",
            display_name="Patron full card — default fields",
            type=list[str],
            default=["barcode", "card_number", "library_name", "subtitle", "patron_name", "expiry"],
            scope="librarian",
            short_help="Fields shown by default on patron full cards.",
            help_text=(
                "Fields shown by default on patron full cards. "
                "Allowed: barcode, card_number, library_name, subtitle, patron_name, expiry, category. "
                "Enter as a comma-separated list."
            ),
            validator=_label_fields_validator("full"),
        )
    )
    register(
        SettingDescriptor(
            key="label_patron_sticker_default_fields",
            display_name="Patron sticker — default fields",
            type=list[str],
            default=["barcode", "card_number"],
            scope="librarian",
            short_help="Fields shown by default on patron stickers.",
            help_text=(
                "Fields shown by default on patron stickers. "
                "Allowed: barcode, card_number, patron_name. "
                "Enter as a comma-separated list."
            ),
            validator=_label_fields_validator("sticker"),
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
            short_help="SMTP server hostname; leave empty to disable email.",
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
            short_help="Use SMTPS implicit TLS (port 465); excludes STARTTLS.",
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
            short_help="Auto-prune old sent/cancelled notifications; empty keeps forever.",
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
            short_help="Default age cutoff for the audit-log prune command.",
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
            short_help="Failed logins per identity allowed before blocking (0 disables).",
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
            short_help="Window (seconds) over which failed logins are counted.",
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
            key="login_max_failures_per_ip",
            display_name="Login Max Failures Per IP",
            type=int,
            default=30,
            scope="system",
            short_help="Failed logins per IP allowed before blocking (0 disables).",
            help_text=(
                "Number of failed login attempts allowed from a single IP "
                "address within the per-IP throttle window before further "
                "attempts from that IP are blocked. Set to 0 to disable "
                "IP-based throttling. Set COMPENDIUM_TRUSTED_PROXIES to "
                "resolve the real client IP behind a reverse proxy."
            ),
            validator=_non_negative_int,
        )
    )
    register(
        SettingDescriptor(
            key="login_failure_window_seconds_per_ip",
            display_name="Login Failure Window Per IP (seconds)",
            type=int,
            default=300,
            scope="system",
            short_help="Window (seconds) over which per-IP failed logins are counted.",
            help_text=(
                "Sliding-window duration for per-IP failed login counting. "
                "Default 300 = 5 minutes."
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
            short_help="Minimum character length for user passwords.",
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
            short_help="bcrypt cost factor for hashing new passwords (10-15).",
            help_text=(
                "bcrypt cost factor used when hashing new passwords. "
                "Higher values are slower and more resistant to brute-force. "
                "Must be between 10 and 15. Existing password hashes are "
                "not affected (bcrypt embeds the cost in each hash)."
            ),
            validator=lambda v: (
                None if 10 <= int(v) <= 15
                else "bcrypt_rounds must be between 10 and 15"
            ),
        )
    )
    # ── Encrypted secrets ──────────────────────────────────────────────────────
    # These are stored encrypted at rest in the DB. Requires COMPENDIUM_SECRET_KEY.
    # The env var still wins on read (COMPENDIUM_SMTP_PASSWORD etc.), so
    # existing env-only deployments are unaffected.
    register(
        SettingDescriptor(
            key="smtp_password",
            display_name="SMTP Password",
            type=str,
            default=None,
            nullable=True,
            scope="system",
            secret=True,
            short_help="Password for the outbound SMTP account.",
            help_text=(
                "Password for the outbound SMTP account. "
                "Stored encrypted at rest. "
                "Environment variable COMPENDIUM_SMTP_PASSWORD takes precedence if set."
            ),
        )
    )
    register(
        SettingDescriptor(
            key="tmdb_api_key",
            display_name="TMDb API Key",
            type=str,
            default=None,
            nullable=True,
            scope="system",
            secret=True,
            short_help="Enables TMDb film/TV metadata.",
            help_text=(
                "API key for The Movie Database (TMDb), used to fetch film and "
                "TV metadata. Obtain one at themoviedb.org → Settings → API. "
                "Stored encrypted at rest. "
                "Environment variable COMPENDIUM_TMDB_API_KEY takes precedence if set."
            ),
        )
    )
    register(
        SettingDescriptor(
            key="google_books_api_key",
            display_name="Google Books API Key",
            type=str,
            default=None,
            nullable=True,
            scope="system",
            secret=True,
            short_help="Enables Google Books as a metadata source.",
            help_text=(
                "API key for the Google Books API. When set and "
                "'book_metadata_source_preference' is 'googlebooks', Google Books "
                "is the primary source for book metadata. "
                "When 'book_metadata_fallback_enabled' is true (the default), the other "
                "source is tried automatically on any miss. "
                "Obtain a key at console.cloud.google.com → APIs & Services → Credentials. "
                "Free tier: 1 000 requests/day. "
                "Stored encrypted at rest. "
                "Environment variable COMPENDIUM_GOOGLE_BOOKS_API_KEY takes precedence if set."
            ),
        )
    )

    def _gb_key_availability_hint() -> "AvailabilityHint | None":
        from compendium.services.site_settings import get_site_setting
        try:
            key = get_site_setting("google_books_api_key")
        except Exception:
            key = None
        if not key:
            return AvailabilityHint(
                unavailable_choices=frozenset({"googlebooks"}),
                warning=(
                    "Google Books requires an API key. Configure the "
                    "Google Books API Key below to use Google Books as primary."
                ),
            )
        return None

    register(
        SettingDescriptor(
            key="book_metadata_source_preference",
            display_name="Book Metadata Source",
            type=Literal["googlebooks", "openlibrary"],
            default="googlebooks",
            scope="system",
            short_help="Which service is tried first for ISBN lookups.",
            help_text=(
                "Primary metadata adapter for books. "
                "'googlebooks' requires an API key (configure the Google Books API Key below); "
                "'openlibrary' is always available without a key. "
                "When 'book_metadata_fallback_enabled' is on, the other source is "
                "tried automatically on any miss — making the fallback symmetric in both directions. "
                "Environment variable COMPENDIUM_BOOK_METADATA_SOURCE_PREFERENCE "
                "takes precedence if set."
            ),
            availability_hint=_gb_key_availability_hint,
        )
    )
    register(
        SettingDescriptor(
            key="book_metadata_fallback_enabled",
            display_name="Enable Fallback to Secondary Source",
            type=bool,
            default=True,
            scope="system",
            short_help="Try the other source automatically on a miss.",
            help_text=(
                "When enabled (the default), if the primary book metadata source returns "
                "no data for an ISBN, the other source is tried automatically. "
                "Disable this to use only the configured primary source with no fallback "
                "(e.g. 'Google Books only, do not try Open Library'). "
                "Environment variable COMPENDIUM_BOOK_METADATA_FALLBACK_ENABLED "
                "takes precedence if set."
            ),
        )
    )
    register(
        SettingDescriptor(
            key="metadata_cache_ttl_days",
            display_name="Metadata Cache TTL (days)",
            type=int,
            default=30,
            scope="system",
            short_help="How long successful lookups are cached before refresh.",
            help_text=(
                "How long to keep successful external metadata lookups cached in "
                "the database (Google Books, Open Library, MusicBrainz, TMDb). "
                "Negative (not-found) responses are always cached for 24 hours. "
                "Set to a higher value to reduce network calls; lower to pick up "
                "upstream corrections sooner. "
                "Environment variable COMPENDIUM_METADATA_CACHE_TTL_DAYS takes precedence if set."
            ),
            validator=_positive_int,
        )
    )


_register_builtins()
