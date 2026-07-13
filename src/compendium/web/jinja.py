from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

from compendium.services.auth import has_permission as _has_permission
from compendium.services.formatting import format_currency as _format_currency
from compendium.services.site_settings import get_site_setting
from compendium.web.nav_pages import NAV_PAGES

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _jinja_has_permission(user, perm: str) -> bool:
    if user is None:
        return False
    return _has_permission(user.role.permissions, perm)


def _jinja_default_theme() -> str:
    return get_site_setting("default_theme")


def _jinja_today_iso() -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        tz = ZoneInfo(get_site_setting("library_timezone"))
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        tz = timezone.utc
    return datetime.now(tz).date().isoformat()


def _jinja_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _jinja_library_name() -> str:
    return get_site_setting("library_name")


def _jinja_custom_shortcuts(user=None) -> list[dict[str, str]]:
    """Site-wide nav shortcuts, filtered to those the current user may access.

    ``custom_shortcuts`` is a single site-wide setting, so without per-user
    filtering a patron would see (and 403 on) shortcuts pointing at staff-only
    pages. Drop any shortcut whose destination page requires a permission the
    user lacks.
    """
    raw: list[str] = get_site_setting("custom_shortcuts") or []
    by_url = {p["url"]: p for p in NAV_PAGES}
    perms = user.role.permissions if user is not None else []
    result: list[dict[str, str]] = []
    for u in raw:
        page = by_url.get(u.strip())
        if page is None:
            continue
        if page["permission"] is not None and not _has_permission(perms, page["permission"]):
            continue
        result.append({"label": page["label"], "url": page["url"]})
    return result


def _jinja_shortcut_pages() -> list[dict]:
    """Full page list — for the admin settings picker."""
    return NAV_PAGES


def _jinja_shortcut_pages_for_user(user) -> list[dict]:
    """Permission-filtered page list — for the per-user nav modal."""
    if user is None:
        return []
    perms = user.role.permissions
    return [
        p for p in NAV_PAGES
        if p["permission"] is None or _has_permission(perms, p["permission"])
    ]


def _jinja_timezone_picker_data(current: str | None = None) -> dict:
    """Data for the settings timezone picker (Region=country, City=zone).

    Returns ``{"groups": [(region, [(zone, label), ...]), ...],
    "selected_region": <label>}`` using the committed by-country grouping in
    ``tz_regions`` (UTC + anglophone locales pinned first). If ``current`` isn't
    one of the canonical picker zones (a legacy alias, or a value set via env or
    the old free-form field), it's surfaced in a one-off ``Current`` group so it
    stays selectable and is never silently rewritten on save.
    """
    from compendium.web.tz_regions import TZ_REGIONS

    groups: list[tuple[str, list[tuple[str, str]]]] = TZ_REGIONS
    selected_region: str | None = None
    if current:
        for label, zones in groups:
            if any(zone == current for zone, _ in zones):
                selected_region = label
                break
        if selected_region is None:
            groups = [("Current", [(current, current)]), *groups]
            selected_region = "Current"
    if selected_region is None:
        selected_region = groups[0][0]
    return {"groups": groups, "selected_region": selected_region}


def _jinja_guest_search_enabled() -> bool:
    return bool(get_site_setting("guest_search_enabled"))


def _jinja_csp_nonce(request) -> str:
    """Return the per-request CSP nonce for inline <script> tags.

    Set by `_SecurityHeadersMiddleware` on `request.state`. Returns an empty
    string if the middleware didn't run (e.g. unit-rendered templates) so
    callers don't crash; CSP-protected pages always have a real value.
    """
    return getattr(request.state, "csp_nonce", "")


templates.env.globals["has_permission"] = _jinja_has_permission
templates.env.globals["default_theme"] = _jinja_default_theme
templates.env.globals["today_iso"] = _jinja_today_iso
templates.env.globals["now"] = _jinja_now
templates.env.globals["library_name"] = _jinja_library_name
templates.env.globals["csp_nonce"] = _jinja_csp_nonce
templates.env.globals["custom_shortcuts"] = _jinja_custom_shortcuts
templates.env.globals["shortcut_pages"] = _jinja_shortcut_pages
templates.env.globals["shortcut_pages_for_user"] = _jinja_shortcut_pages_for_user
templates.env.globals["guest_search_enabled"] = _jinja_guest_search_enabled
templates.env.globals["timezone_picker_data"] = _jinja_timezone_picker_data
templates.env.filters["currency"] = _format_currency
