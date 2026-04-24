from datetime import date, datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

from compendium.config.settings import Settings
from compendium.services.auth import has_permission as _has_permission
from compendium.services.formatting import format_currency as _format_currency

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _jinja_has_permission(user, perm: str) -> bool:
    if user is None:
        return False
    return _has_permission(user.role.permissions, perm)


def _jinja_default_theme() -> str:
    return Settings().default_theme


def _jinja_today_iso() -> str:
    return date.today().isoformat()


def _jinja_now() -> datetime:
    return datetime.now(tz=timezone.utc)


templates.env.globals["has_permission"] = _jinja_has_permission
templates.env.globals["default_theme"] = _jinja_default_theme
templates.env.globals["today_iso"] = _jinja_today_iso
templates.env.globals["now"] = _jinja_now
templates.env.filters["currency"] = _format_currency
