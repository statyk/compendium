from pathlib import Path

_TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent
    / "src" / "compendium" / "web" / "templates"
)


def test_templates_use_permission_helper():
    """All templates should use the has_permission(user, perm) Jinja global
    for permission checks, rather than hand-rolling the
    '"*" in user.role.permissions' role/permissions-list check inline."""
    for path in sorted(_TEMPLATES_DIR.rglob("*.html")):
        src = path.read_text()
        assert '"*" in user.role.permissions' not in src, (
            f"{path.relative_to(_TEMPLATES_DIR)} hand-rolls the permission "
            "check instead of using has_permission(user, perm)"
        )
