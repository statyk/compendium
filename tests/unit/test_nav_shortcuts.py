"""Unit tests for permission-filtering of the site-wide nav shortcuts.

`custom_shortcuts` is a single site-wide setting; without per-user filtering a
patron (or guest) would see shortcuts pointing at staff-only pages and 403 on
click. `_jinja_custom_shortcuts(user)` must drop any shortcut whose destination
requires a permission the user lacks.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from compendium.web import jinja


def _user(*perms: str):
    return SimpleNamespace(role=SimpleNamespace(permissions=list(perms)))


# A mix: open page (Catalog, no perm), a staff page (Circ Desk → loan.checkout),
# and a self-service page (My Fines → fine.view.self).
_SHORTCUTS = ["/ui/catalog", "/ui/circ", "/ui/me/fines"]


def _shortcuts_for(user):
    with patch.object(jinja, "get_site_setting", return_value=_SHORTCUTS):
        return jinja._jinja_custom_shortcuts(user)


def _urls(rows):
    return [r["url"] for r in rows]


def test_patron_only_sees_permitted_shortcuts():
    # Patron preset perms: item/work view + self-service, no loan.checkout.
    patron = _user("item.view", "work.view", "fine.view.self")
    urls = _urls(_shortcuts_for(patron))
    assert "/ui/catalog" in urls
    assert "/ui/me/fines" in urls
    assert "/ui/circ" not in urls  # staff-only — must be filtered out


def test_guest_only_sees_unrestricted_shortcuts():
    urls = _urls(_shortcuts_for(None))
    assert urls == ["/ui/catalog"]  # only the no-permission page survives


def test_admin_wildcard_sees_all_shortcuts():
    admin = _user("*")
    urls = _urls(_shortcuts_for(admin))
    assert urls == _SHORTCUTS


def test_unknown_url_is_dropped():
    with patch.object(jinja, "get_site_setting", return_value=["/ui/bogus", "/ui/catalog"]):
        urls = _urls(jinja._jinja_custom_shortcuts(_user("*")))
    assert urls == ["/ui/catalog"]
