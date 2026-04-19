import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from compendium.api.routes import audit, auth, branches, holds, items, loans, me, patrons, policies, users, works
from compendium.config.settings import INSECURE_JWT_DEFAULT
from compendium.db.engine import get_settings
from compendium.web.app import NoPatronAccountException, RequiresLoginException, create_web_router
from compendium.web.jinja import templates

_WEB_STATIC = Path(__file__).parent.parent / "web" / "static"
_log = logging.getLogger("compendium")


def create_app() -> FastAPI:
    if get_settings().jwt_secret_key == INSECURE_JWT_DEFAULT:
        _log.warning(
            "SECURITY: COMPENDIUM_JWT_SECRET_KEY is set to the insecure default. "
            "Set it to a random secret before exposing this server to the network."
        )

    app = FastAPI(title="Compendium", version="0.1.0")

    # JSON API routes
    app.include_router(branches.router, prefix="/branches", tags=["branches"])
    app.include_router(audit.router, prefix="/audit", tags=["audit"])
    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(works.router, prefix="/works", tags=["works"])
    app.include_router(items.router, prefix="/items", tags=["items"])
    app.include_router(patrons.router, prefix="/patrons", tags=["patrons"])
    app.include_router(loans.router, prefix="/loans", tags=["loans"])
    app.include_router(holds.router, prefix="/holds", tags=["holds"])
    app.include_router(policies.router, prefix="/policies", tags=["policies"])
    app.include_router(me.router, prefix="/me", tags=["me"])
    app.include_router(users.router, prefix="/users", tags=["users"])

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
        from compendium.db.session import get_session
        from compendium.repositories.sql.user_repository import SqlUserRepository
        from compendium.web.deps import AUTH_COOKIE, _decode_token

        user = None
        username = None
        token = request.cookies.get(AUTH_COOKIE)
        session_gen = None
        try:
            if token:
                payload = _decode_token(token)
                if payload:
                    username = payload.get("username")
                    session_gen = get_session()
                    session = next(session_gen)
                    user = SqlUserRepository(session).get(int(payload["sub"]))
                    if user is not None and not user.is_active:
                        user = None
                    if user is not None:
                        _ = user.role.permissions  # force lazy load before session close
            return templates.TemplateResponse(
                request,
                "error_no_patron.html",
                {"user": user, "csrf_token": "", "username": username},
                status_code=403,
            )
        finally:
            if session_gen is not None:
                session_gen.close()

    return app
