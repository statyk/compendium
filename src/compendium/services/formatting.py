"""Currency + small display-formatting helpers.

Kept framework-free so it can be imported from domain, services, CLI, or web.
"""

from __future__ import annotations

from compendium.config.settings import Settings


def format_currency(cents: int, settings: Settings | None = None) -> str:
    """Format an integer cent amount as currency using the configured symbol
    and position. Decimal separator is always '.' (full locale-aware formatting
    is out of scope for v1)."""
    s = settings or _get_settings()
    sign = "-" if cents < 0 else ""
    dollars = f"{abs(cents) / 100:.2f}"
    if s.currency_symbol_position == "after":
        return f"{sign}{dollars} {s.currency_symbol}"
    return f"{sign}{s.currency_symbol}{dollars}"


def _get_settings() -> Settings:
    # Imported lazily to avoid a hard dependency on the configured Settings
    # singleton when tests want to pass an explicit instance.
    from compendium.db.engine import get_settings

    return get_settings()
