"""Web UI — HTMX + Jinja2 frontend, registered as an APIRouter on the main FastAPI app."""

from __future__ import annotations

from fastapi import APIRouter

from compendium.web.deps import RequiresLoginException
from compendium.web.routes import auth, catalog, circ, me, patrons


def create_web_router() -> APIRouter:
    router = APIRouter()
    router.include_router(auth.router)
    router.include_router(catalog.router)
    router.include_router(circ.router)
    router.include_router(me.router)
    router.include_router(patrons.router)
    return router


__all__ = ["create_web_router", "RequiresLoginException"]
