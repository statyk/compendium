"""Web UI — HTMX + Jinja2 frontend, registered as an APIRouter on the main FastAPI app."""

from __future__ import annotations

from fastapi import APIRouter

from compendium.web.deps import NoPatronAccountException, RequiresLoginException
from compendium.web.routes import (
    admin_circulation,
    admin_holds,
    admin_settings,
    audit,
    auth,
    branches,
    bulk,
    catalog,
    circ,
    covers,
    creators,
    curated_lists,
    fines,
    households,
    items,
    kiosk,
    labels,
    library_hours,
    me,
    notifications,
    patron_categories,
    patrons,
    policies,
    reports,
    roles,
    scan,
    trash,
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
    router.include_router(patron_categories.router)
    router.include_router(policies.router)
    router.include_router(roles.router)
    router.include_router(users.router)
    router.include_router(audit.router)
    router.include_router(bulk.router)
    router.include_router(fines.router)
    router.include_router(notifications.router)
    router.include_router(covers.router)
    router.include_router(reports.router)
    router.include_router(kiosk.router)
    router.include_router(scan.router)
    router.include_router(labels.router)
    router.include_router(admin_holds.router)
    router.include_router(admin_circulation.router)
    router.include_router(admin_settings.router)
    router.include_router(library_hours.router)
    router.include_router(households.router)
    router.include_router(curated_lists.router)
    router.include_router(trash.router)
    return router


__all__ = ["create_web_router", "NoPatronAccountException", "RequiresLoginException"]
