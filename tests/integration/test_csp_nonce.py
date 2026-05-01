"""CSP nonce hardening (M3).

Three layers of protection so a future contributor can't accidentally
re-introduce `'unsafe-inline'`-style script execution:

1. Middleware test: every response carries a unique nonce, the CSP doesn't
   allow `'unsafe-inline'` for scripts, and includes `'strict-dynamic'`.
2. Template-walk test: any inline `<script>` block in `web/templates/`
   must include a `nonce=` attribute. Catches missed templates at test
   time, before the page silently breaks in a browser.
3. Smoke test: a real rendered page's `<script nonce=X>` matches the CSP
   header's `'nonce-X'` — i.e. the wiring actually works end-to-end.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from compendium.api.app import create_app
from compendium.config.seed import seed_defaults
from compendium.db.session import get_session
from compendium.domain.models import Base
from tests.helpers import setup_sqlite_fts

_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "src" / "compendium" / "web" / "templates"
# Match an opening <script ...> tag that is NOT a self-closing/external tag
# (i.e. has no `src=` attribute). The DOTALL+greedy on attributes handles
# multi-line attributes too.
_INLINE_SCRIPT_RE = re.compile(r"<script\b(?P<attrs>[^>]*)>", re.IGNORECASE)


def _has_nonce(attrs: str) -> bool:
    return bool(re.search(r"\bnonce\s*=", attrs))


def test_every_script_in_templates_has_nonce():
    """Both inline and external <script> tags need nonces under 'strict-dynamic'.

    'strict-dynamic' causes browsers to ignore 'self' and other allowlist
    sources for scripts, so every <script> tag — src= or inline — must carry
    a nonce to execute.
    """
    offenders: list[str] = []
    for path in _TEMPLATES_DIR.rglob("*.html"):
        text = path.read_text()
        for match in _INLINE_SCRIPT_RE.finditer(text):
            attrs = match.group("attrs")
            if not _has_nonce(attrs):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(_TEMPLATES_DIR)}:{line} → {match.group(0)}")
    assert not offenders, (
        "Every <script> tag in web/templates/ (inline and src=) must carry "
        'nonce="{{ csp_nonce(request) }}" because \'strict-dynamic\' overrides '
        "'self'. Missing nonces:\n  "
        + "\n  ".join(offenders)
    )


@pytest.fixture()
def client():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    setup_sqlite_fts(eng)
    factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    session = factory()
    seed_defaults(session)
    session.commit()

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app, follow_redirects=False) as c:
        yield c
    session.close()


def test_csp_header_drops_unsafe_inline_for_scripts(client):
    resp = client.get("/ui/login")
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    # script-src section excludes 'unsafe-inline' and includes a nonce + strict-dynamic.
    assert "'unsafe-inline'" not in _script_src(csp), csp
    assert "'strict-dynamic'" in _script_src(csp), csp
    assert "'nonce-" in _script_src(csp), csp


def test_each_request_gets_a_fresh_nonce(client):
    n1 = _extract_nonce(client.get("/ui/login").headers["content-security-policy"])
    n2 = _extract_nonce(client.get("/ui/login").headers["content-security-policy"])
    assert n1 and n2 and n1 != n2


def test_rendered_script_nonce_matches_csp_nonce(client):
    resp = client.get("/ui/login")
    csp_nonce = _extract_nonce(resp.headers["content-security-policy"])
    body = resp.text
    # /ui/login extends base.html which has the theme-prepaint inline script.
    body_nonces = re.findall(r'<script\s+nonce="([^"]+)"', body)
    assert body_nonces, "expected at least one inline <script nonce=...> in /ui/login"
    for nonce in body_nonces:
        assert nonce == csp_nonce, (
            f"inline <script nonce={nonce}> doesn't match CSP nonce={csp_nonce}"
        )


def _script_src(csp: str) -> str:
    for directive in csp.split(";"):
        directive = directive.strip()
        if directive.startswith("script-src"):
            return directive
    return ""


def _extract_nonce(csp: str) -> str | None:
    m = re.search(r"'nonce-([^']+)'", _script_src(csp))
    return m.group(1) if m else None
