import re
from pathlib import Path

_TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent
    / "src" / "compendium" / "web" / "templates"
)

# Leading \s boundary avoids false positives:
#  - hx-on:click=  (preceded by '-')
#  - value="on"    (preceded by '"')
#  - aria-...      (preceded by '-')
# Catches every HTML event-handler attribute (onclick, onchange, onsubmit,
# onload, onerror, onfocus, onblur, oninput, onkey*, onmouse*, onpointer*, ...)
# without needing to enumerate them.
_HANDLER_RE = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)

_JS_URL_RE = re.compile(
    r"""(?:href|src|action|formaction)\s*=\s*["']\s*javascript:""",
    re.IGNORECASE,
)


def test_no_inline_event_handlers_in_templates():
    """Inline event-handler attributes (onclick=, onchange=, etc.) are
    silently dropped by the project's nonce-based CSP. Use HTMX hx-confirm /
    hx-on::event, event delegation in a nonced <script> block, or a
    server-side confirm page instead. See docs/architecture.md
    "CSP and inline scripts" for examples."""
    offenders = []
    for path in sorted(_TEMPLATES_DIR.rglob("*.html")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for m in _HANDLER_RE.finditer(line):
                offenders.append(
                    f"{path.relative_to(_TEMPLATES_DIR)}:{lineno} "
                    f"→ {m.group(0).strip()}"
                )
    assert not offenders, (
        "Inline event-handler attributes silently fail under the project's "
        "nonce-based CSP. Replace with hx-confirm / hx-on::event / "
        "event delegation in a nonced <script>:\n  "
        + "\n  ".join(offenders)
    )


def test_no_javascript_urls_in_templates():
    """`javascript:` URLs in href/src/action/formaction are blocked the same
    way; treat them as event handlers."""
    offenders = []
    for path in sorted(_TEMPLATES_DIR.rglob("*.html")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _JS_URL_RE.search(line):
                offenders.append(f"{path.relative_to(_TEMPLATES_DIR)}:{lineno}")
    assert not offenders, (
        "`javascript:` URLs are blocked by the project's CSP "
        "(no 'unsafe-inline' for scripts):\n  " + "\n  ".join(offenders)
    )
