from pathlib import Path

_TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent
    / "src" / "compendium" / "web" / "templates"
)


def test_items_detail_uses_permission_helper():
    """items/detail.html should use the has_permission(user, perm) Jinja
    global like every sibling permission check in the file, rather than
    hand-rolling the role/permissions-list check inline."""
    src = (_TEMPLATES_DIR / "items" / "detail.html").read_text()
    assert '"*" in user.role.permissions' not in src
