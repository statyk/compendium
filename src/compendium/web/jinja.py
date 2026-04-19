from pathlib import Path

from fastapi.templating import Jinja2Templates

from compendium.services.auth import has_permission as _has_permission

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _jinja_has_permission(user, perm: str) -> bool:
    if user is None:
        return False
    return _has_permission(user.role.permissions, perm)


templates.env.globals["has_permission"] = _jinja_has_permission
