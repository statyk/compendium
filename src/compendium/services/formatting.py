"""Currency + small display-formatting helpers.

Kept framework-free so it can be imported from domain, services, CLI, or web.
"""

from __future__ import annotations


def format_currency(cents: int) -> str:
    """Format an integer cent amount as currency using the configured symbol
    and position. Decimal separator is always '.' (full locale-aware formatting
    is out of scope for v1)."""
    # Imported lazily to keep this module framework-free and avoid pulling in
    # the SQLAlchemy stack from settings_registry until first call.
    from compendium.services.site_settings import get_site_setting

    symbol = get_site_setting("currency_symbol")
    position = get_site_setting("currency_symbol_position")
    sign = "-" if cents < 0 else ""
    dollars = f"{abs(cents) / 100:.2f}"
    if position == "after":
        return f"{sign}{dollars} {symbol}"
    return f"{sign}{symbol}{dollars}"
