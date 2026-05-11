"""Librarian-facing hold listing + work queue views."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from compendium.db.session import get_session
from compendium.domain.enums import HoldStatus
from compendium.domain.models import AppUser
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.web.csrf import ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "hold.view.any"
_PAGE_SIZE = 50


def _render(name: str, request: Request, ctx: dict):
    from compendium.db.engine import get_settings

    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean)
    if fresh:
        set_csrf_cookie(resp, fresh)
    return resp


@router.get("/admin/holds")
def admin_holds(
    request: Request,
    status: str | None = Query(default=None),
    branch: str | None = Query(default=None),
    q: str | None = Query(default=None),
    older_than_days: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    status_clean = (status or "").strip() or None
    query_clean = (q or "").strip() or None
    branch_clean = (branch or "").strip() or None

    branch_id: int | None = None
    if branch_clean:
        b = SqlBranchRepository(session).get_by_code(branch_clean)
        branch_id = b.id if b is not None else -1  # -1 forces empty result set

    repo = SqlHoldRepository(session)
    offset = (page - 1) * _PAGE_SIZE
    holds = repo.list_active(
        status=status_clean,
        branch_id=branch_id,
        query=query_clean,
        older_than_days=older_than_days,
        limit=_PAGE_SIZE,
        offset=offset,
    )
    total = repo.count_active(
        status=status_clean,
        branch_id=branch_id,
        query=query_clean,
        older_than_days=older_than_days,
    )

    # Compute queue position for each returned hold (small N per page).
    positions = {h.id: repo.queue_position(h.id) for h in holds}

    has_prev = page > 1
    has_next = offset + len(holds) < total

    # Preserve filters in pagination links
    params = {}
    if status_clean:
        params["status"] = status_clean
    if branch_clean:
        params["branch"] = branch_clean
    if query_clean:
        params["q"] = query_clean
    if older_than_days is not None:
        params["older_than_days"] = str(older_than_days)
    filters_qs = urlencode(params)

    branches = SqlBranchRepository(session).list()

    return _render(
        "admin/holds.html",
        request,
        {
            "request": request,
            "user": user,
            "holds": holds,
            "positions": positions,
            "total": total,
            "page": page,
            "has_prev": has_prev,
            "has_next": has_next,
            "filters_qs": filters_qs,
            "status": status_clean or "",
            "branch": branch_clean or "",
            "q": query_clean or "",
            "older_than_days": older_than_days,
            "branches": branches,
            "status_options": [
                ("", "All active"),
                (HoldStatus.WAITING.value, "Waiting"),
                (HoldStatus.AVAILABLE.value, "Pickup shelf"),
            ],
        },
    )
