import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from compendium.api.routes import (
    audit,
    auth,
    branches,
    calendar as calendar_routes,
    creators,
    fines,
    holds,
    households,
    imports,
    items,
    labels,
    loans,
    me,
    notifications as api_notifications,
    patron_categories,
    patrons,
    policies,
    reports,
    settings as settings_routes,
    users,
    works,
)
from compendium.config.settings import INSECURE_JWT_DEFAULT, MIN_JWT_SECRET_LENGTH, InsecureConfigError
from compendium.db.engine import get_settings
from compendium.web.app import NoPatronAccountException, RequiresLoginException, create_web_router
from compendium.web.jinja import templates

_WEB_STATIC = Path(__file__).parent.parent / "web" / "static"
_log = logging.getLogger("compendium")


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply baseline security headers to every response.

    Generates a per-request CSP nonce (`request.state.csp_nonce`) that
    templates inject into legitimate `<script>` tags via the `csp_nonce()`
    Jinja global. The CSP itself drops `'unsafe-inline'` for scripts in
    favor of `'nonce-...' 'strict-dynamic'`, so a comment-field XSS can't
    smuggle a script even if it slips past output sanitization. Style-src
    still allows inline because templates use `style="..."` attributes
    extensively; cosmetic CSS XSS is much lower impact.

    A test in `tests/integration/test_csp_nonce.py` walks the templates
    directory and asserts every inline `<script>` block has `nonce=` —
    miss one and the test fails before the page silently breaks.
    """

    async def dispatch(self, request: Request, call_next):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https://covers.openlibrary.org "
            "https://image.tmdb.org; "
            f"script-src 'self' 'nonce-{nonce}' 'strict-dynamic'; "
            "style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; worker-src 'self' blob:",
        )
        return response


def _warn_if_no_system_admin() -> None:
    """Log a warning if no active user holds the system.manage permission.

    Doesn't raise — a deployment mid-migration legitimately may not have one
    yet, and a hard fail would lock the operator out. Best-effort: we open a
    short-lived session and swallow exceptions (table missing, DB unreachable).
    """
    try:
        from sqlalchemy.orm import Session

        from compendium.db.engine import get_engine
        from compendium.domain.models import AppUser
        from compendium.services.auth import has_permission

        with Session(get_engine()) as s:
            users = (
                s.query(AppUser)
                .filter(AppUser.is_active == True)  # noqa: E712
                .all()
            )
            for u in users:
                if u.role and has_permission(u.role.permissions, "system.manage"):
                    return
            if users:
                _log.warning(
                    "No active user holds 'system.manage'. Infrastructure "
                    "settings (slice C: SMTP, retention, etc.) will be "
                    "unmanageable from the UI. Assign Administrator or "
                    "SystemAdmin to at least one active user."
                )
    except Exception:  # pragma: no cover — startup convenience only
        _log.debug("system.manage holder check skipped (DB not ready?)")


def create_app() -> FastAPI:
    secret = get_settings().jwt_secret_key
    allow_insecure = os.environ.get("COMPENDIUM_ALLOW_INSECURE_JWT") == "1"
    is_default = secret == INSECURE_JWT_DEFAULT
    is_too_short = len(secret) < MIN_JWT_SECRET_LENGTH
    if is_default or is_too_short:
        reason = (
            "set to the insecure default"
            if is_default
            else f"shorter than the {MIN_JWT_SECRET_LENGTH}-character minimum"
        )
        if allow_insecure:
            _log.warning(
                "SECURITY: COMPENDIUM_JWT_SECRET_KEY is %s. "
                "COMPENDIUM_ALLOW_INSECURE_JWT=1 is set, so the server is starting "
                "anyway. DO NOT do this in production.",
                reason,
            )
        else:
            raise InsecureConfigError(
                f"COMPENDIUM_JWT_SECRET_KEY is {reason}. Generate one with "
                '`python -c "import secrets; print(secrets.token_urlsafe(48))"` '
                "(or `compendium keygen --jwt`). For first-run/dev only, "
                "you may set COMPENDIUM_ALLOW_INSECURE_JWT=1 to bypass."
            )
    _warn_if_no_system_admin()

    app = FastAPI(title="Compendium", version="0.1.0")
    app.add_middleware(_SecurityHeadersMiddleware)
    allowed_hosts_raw = get_settings().allowed_hosts
    if allowed_hosts_raw:
        hosts = [h.strip() for h in allowed_hosts_raw.split(",") if h.strip()]
        if hosts:
            app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    # JSON API routes
    app.include_router(branches.router, prefix="/branches", tags=["branches"])
    app.include_router(audit.router, prefix="/audit", tags=["audit"])
    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(works.router, prefix="/works", tags=["works"])
    app.include_router(creators.router, prefix="/creators", tags=["creators"])
    app.include_router(items.router, prefix="/items", tags=["items"])
    app.include_router(patrons.router, prefix="/patrons", tags=["patrons"])
    app.include_router(
        patron_categories.router, prefix="/patron-categories", tags=["patron-categories"]
    )
    app.include_router(loans.router, prefix="/loans", tags=["loans"])
    app.include_router(holds.router, prefix="/holds", tags=["holds"])
    app.include_router(policies.router, prefix="/policies", tags=["policies"])
    app.include_router(me.router, prefix="/me", tags=["me"])
    app.include_router(users.router, prefix="/users", tags=["users"])
    app.include_router(imports.import_router, prefix="/import", tags=["import"])
    app.include_router(imports.export_router, prefix="/export", tags=["export"])
    app.include_router(fines.fines_router, prefix="/fines", tags=["fines"])
    app.include_router(fines.patron_fines_router, prefix="/patrons", tags=["fines"])
    app.include_router(fines.me_fines_router, prefix="/me", tags=["fines"])
    app.include_router(fines.items_lifecycle_router, prefix="/items", tags=["fines"])
    app.include_router(api_notifications.router, prefix="/notifications", tags=["notifications"])
    app.include_router(reports.router, prefix="/reports", tags=["reports"])
    app.include_router(labels.router, prefix="/labels", tags=["labels"])
    app.include_router(settings_routes.router, prefix="/settings", tags=["settings"])
    app.include_router(calendar_routes.router, tags=["calendar"])
    app.include_router(households.router, prefix="/households", tags=["households"])

    # Web UI routes (HTMX + Jinja2)
    app.mount("/ui/static", StaticFiles(directory=str(_WEB_STATIC)), name="web_static")
    app.include_router(create_web_router(), prefix="/ui", tags=["web"])

    @app.exception_handler(RequiresLoginException)
    async def _login_redirect(request: Request, exc: RequiresLoginException) -> RedirectResponse:
        url = "/ui/login"
        if exc.next_url:
            url = f"/ui/login?next={exc.next_url}"
        return RedirectResponse(url=url, status_code=303)

    @app.exception_handler(NoPatronAccountException)
    async def _no_patron(request: Request, exc: NoPatronAccountException) -> HTMLResponse:
        from types import SimpleNamespace

        from sqlalchemy.orm import Session

        from compendium.db.engine import get_engine
        from compendium.repositories.sql.user_repository import SqlUserRepository
        from compendium.web.csrf import ensure_csrf, set_csrf_cookie
        from compendium.web.deps import AUTH_COOKIE, _decode_token

        user = None
        username = None
        token = request.cookies.get(AUTH_COOKIE)
        if token:
            payload = _decode_token(token)
            if payload:
                username = payload.get("username")
                # Attempt to reload fresh permissions from the DB so they
                # reflect any role change since the JWT was issued.  Fall
                # back to the JWT payload snapshot on any failure so the
                # page always renders (tests use a separate StaticPool
                # engine that this handler can't reach via get_engine()).
                permissions = None
                try:
                    with Session(get_engine()) as s:
                        db_user = SqlUserRepository(s).get_by_username(username)
                        if db_user is not None and db_user.role is not None:
                            permissions = list(db_user.role.permissions)
                except Exception:
                    pass
                user = SimpleNamespace(
                    username=username,
                    role=SimpleNamespace(
                        permissions=permissions
                        if permissions is not None
                        else payload.get("permissions", [])
                    ),
                )
        csrf_token, fresh = ensure_csrf(request)
        response = templates.TemplateResponse(
            request,
            "error_no_patron.html",
            {"user": user, "csrf_token": csrf_token, "username": username},
            status_code=403,
        )
        if fresh:
            set_csrf_cookie(response, fresh)
        return response

    return app
