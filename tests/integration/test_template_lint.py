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


REQUIRED_MARKER = '<span class="required-marker" aria-hidden="true">*</span>'


def test_required_markers_canonical():
    """Required-field markers should use the canonical
    '<span class="required-marker" aria-hidden="true">*</span>' pattern
    everywhere, and the mixed-input create forms should mark their
    required fields with it."""
    for path in sorted(_TEMPLATES_DIR.rglob("*.html")):
        src = path.read_text()
        assert "<sup>*</sup>" not in src, (
            f"{path.relative_to(_TEMPLATES_DIR)} uses a stray <sup>*</sup> "
            "required marker instead of the canonical .required-marker span"
        )
        assert 'style="color:red">*' not in src, (
            f"{path.relative_to(_TEMPLATES_DIR)} uses a stray "
            'style="color:red">* required marker instead of the canonical '
            ".required-marker span"
        )
    for name in (
        "patrons/new.html",
        "users/new.html",
        "policies/new.html",
        "roles/new.html",
        "curated_lists/new.html",
        "households/new.html",
    ):
        path = _TEMPLATES_DIR / name
        assert REQUIRED_MARKER in path.read_text(), (
            f"{name} is a mixed required/optional form and should mark its "
            "required fields with the canonical .required-marker span"
        )


def test_me_pages_rehome_focus_after_swap():
    """After a self-service loan/hold row swaps via HTMX, keyboard focus is
    lost to <body>. The enclosing row container re-homes focus to the
    swapped row's first interactive element via hx-on::after-swap.

    Rows swap with hx-swap="outerHTML", so htmx:afterSwap's
    event.detail.target still points at the detached pre-swap <tr> —
    event.detail.elt (the new element the event is dispatched on) is the
    only property that actually works here. Pin the working form so a
    regression back to .target fails this test rather than being a silent
    no-op discovered only in a browser."""
    for name in ("me/loans.html", "me/holds.html"):
        src = (_TEMPLATES_DIR / name).read_text()
        assert "hx-on::after-swap" in src, name
        assert "event.detail.elt" in src, name
