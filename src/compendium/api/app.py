from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from compendium.api.routes import auth, holds, items, loans, me, patrons, policies, users, works
from compendium.web.app import RequiresLoginException, create_web_router

_WEB_STATIC = Path(__file__).parent.parent / "web" / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="Compendium", version="0.1.0")

    # JSON API routes
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

    return app
