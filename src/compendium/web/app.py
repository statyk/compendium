"""Web UI — HTMX + Jinja2 frontend, registered as an APIRouter on the main FastAPI app."""

from __future__ import annotations

from fastapi import APIRouter

from compendium.web.deps import NoPatronAccountException, RequiresLoginException
from compendium.web.routes import (
    audit,
    auth,
    branches,
    bulk,
    catalog,
    circ,
    covers,
    creators,
    fines,
    items,
    me,
    notifications,
    patrons,
    policies,
    roles,
    users,
)


def create_web_router() -> APIRouter:
    router = APIRouter()
    router.include_router(auth.router)
    router.include_router(branches.router)
    router.include_router(catalog.router)
    router.include_router(creators.router)
    router.include_router(circ.router)
    router.include_router(items.router)
    router.include_router(me.router)
    router.include_router(patrons.router)
    router.include_router(policies.router)
    router.include_router(roles.router)
    router.include_router(users.router)
    router.include_router(audit.router)
    router.include_router(bulk.router)
    router.include_router(fines.router)
    router.include_router(notifications.router)
    router.include_router(covers.router)
    return router


__all__ = ["create_web_router", "NoPatronAccountException", "RequiresLoginException"]
