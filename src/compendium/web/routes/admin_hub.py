"""The /ui/admin hub — single home for settings and entity administration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from compendium.domain.models import AppUser
from compendium.services.auth import has_permission
from compendium.web.csrf import ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_user
from compendium.web.jinja import templates
from compendium.web.routes.admin_settings import SETTINGS_PAGES

router = APIRouter()


def _entry(url: str, title: str, intro: str, perm: str) -> dict[str, str]:
    return {"url": url, "title": title, "intro": intro, "perm": perm}


def _settings_entries(tier: str) -> list[dict[str, Any]]:
    return [
        _entry(p["url"], p["title"], p["intro"], p["scope_perm"])
        for p in SETTINGS_PAGES
        if p["tier"] == tier
    ]


# Grouped registry; order here is the display order on the hub AND in the
# merged nav dropdown (base.html mirrors it).
ADMIN_HUB_GROUPS: list[tuple[str, list[dict[str, Any]]]] = [
    (
        "Circulation & policies",
        [
            _entry("/ui/policies", "Loan Policies",
                   "Loan periods, renewal limits, and fine rates.", "policy.edit"),
            _entry("/ui/admin/library-hours", "Library Hours",
                   "Weekly open/close times used for due-date rolling.", "calendar.manage"),
            _entry("/ui/admin/closed-dates", "Closed Dates",
                   "Holidays and one-off closures.", "calendar.manage"),
            _entry("/ui/admin/patron-categories", "Patron Categories",
                   "Borrower categories that drive per-category policies.", "patron.manage"),
        ],
    ),
    (
        "Catalog & library",
        [
            _entry("/ui/branches", "Branches",
                   "Branch names, location codes, and defaults.", "branch.edit"),
            _entry("/ui/curated-lists", "Curated Lists",
                   "Librarian-curated shelves for the catalog landing page.", "curatedlist.manage"),
            _entry("/ui/trash", "Recently Deleted",
                   "Restore or permanently purge deleted works.", "work.delete"),
        ],
    ),
    ("Settings", _settings_entries("librarian")),
    (
        "System",
        _settings_entries("system")
        + [
            _entry("/ui/users", "Users",
                   "Staff and patron login accounts.", "user.manage"),
            _entry("/ui/roles", "Roles",
                   "Permission bundles assigned to users.", "role.manage"),
            _entry("/ui/audit", "Audit Log",
                   "Librarian-level changes to catalog and patron records.", "audit.view"),
        ],
    ),
    (
        "Insights",
        [
            _entry("/ui/reports", "Reports",
                   "Circulation, collection, overdue, and inventory reports.", "report.view"),
            _entry("/ui/admin/notifications", "Notifications",
                   "Email notice templates and delivery log.", "notification.manage"),
        ],
    ),
]


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean, status_code=status_code)
    if fresh:
        set_csrf_cookie(resp, fresh)
    return resp


@router.get("/admin")
def admin_hub(
    request: Request,
    user: AppUser = Depends(require_web_user),
):
    groups = []
    for heading, entries in ADMIN_HUB_GROUPS:
        visible = [
            e for e in entries
            if has_permission(user.role.permissions, e["perm"])
        ]
        if visible:
            groups.append((heading, visible))
    if not groups:
        raise HTTPException(status_code=403, detail="Forbidden")
    return _render(
        "admin/index.html",
        request,
        {"request": request, "user": user, "groups": groups},
    )
