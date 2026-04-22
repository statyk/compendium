import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from compendium.api.routes import (
    audit,
    auth,
    branches,
    creators,
    fines,
    holds,
    imports,
    items,
    loans,
    me,
    patrons,
    policies,
    users,
    works,
)
from compendium.config.settings import INSECURE_JWT_DEFAULT
from compendium.db.engine import get_settings
from compendium.web.app import NoPatronAccountException, RequiresLoginException, create_web_router
from compendium.web.jinja import templates

_WEB_STATIC = Path(__file__).parent.parent / "web" / "static"
_log = logging.getLogger("compendium")


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply baseline security headers to every response.

    CSP includes a small allowance for the inline JS used in a few HTMX partials
    and the ZXing WebAssembly worker. Tighten once inline scripts are eliminated.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https://covers.openlibrary.org "
            "https://image.tmdb.org; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; worker-src 'self' blob:",
        )
        return response


def create_app() -> FastAPI:
    if get_settings().jwt_secret_key == INSECURE_JWT_DEFAULT:
        _log.warning(
            "SECURITY: COMPENDIUM_JWT_SECRET_KEY is set to the insecure default. "
            "Set it to a random secret before exposing this server to the network."
        )

    app = FastAPI(title="Compendium", version="0.1.0")
    app.add_middleware(_SecurityHeadersMiddleware)

    # JSON API routes
    app.include_router(branches.router, prefix="/branches", tags=["branches"])
    app.include_router(audit.router, prefix="/audit", tags=["audit"])
    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(works.router, prefix="/works", tags=["works"])
    app.include_router(creators.router, prefix="/creators", tags=["creators"])
    app.include_router(items.router, prefix="/items", tags=["items"])
    app.include_router(patrons.router, prefix="/patrons", tags=["patrons"])
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

        from compendium.web.csrf import ensure_csrf, set_csrf_cookie
        from compendium.web.deps import AUTH_COOKIE, _decode_token

        # Build a lightweight user view from the JWT payload so the nav can
        # render without a DB round-trip. The exception handler runs outside
        # FastAPI DI, so we can't rely on a request-scoped session.
        user = None
        username = None
        token = request.cookies.get(AUTH_COOKIE)
        if token:
            payload = _decode_token(token)
            if payload:
                username = payload.get("username")
                user = SimpleNamespace(
                    username=username,
                    role=SimpleNamespace(permissions=payload.get("permissions", [])),
                )
        csrf_token, fresh = ensure_csrf(request)
        response = templates.TemplateResponse(
            request,
            "error_no_patron.html",
            {"user": user, "csrf_token": csrf_token, "username": username},
            status_code=403,
        )
        if fresh:
            set_csrf_cookie(response, fresh, get_settings().jwt_secret_key)
        return response

    return app
