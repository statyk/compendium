from datetime import date, datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

from compendium.services.auth import has_permission as _has_permission
from compendium.services.formatting import format_currency as _format_currency
from compendium.services.site_settings import get_site_setting

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _jinja_has_permission(user, perm: str) -> bool:
    if user is None:
        return False
    return _has_permission(user.role.permissions, perm)


def _jinja_default_theme() -> str:
    return get_site_setting("default_theme")


def _jinja_today_iso() -> str:
    return date.today().isoformat()


def _jinja_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _jinja_library_name() -> str:
    return get_site_setting("library_name")


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
templates.env.filters["currency"] = _format_currency
