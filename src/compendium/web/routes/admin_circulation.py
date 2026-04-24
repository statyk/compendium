"""Librarian-facing system-wide circulation views: all active loans,
all outstanding fines."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from compendium.db.session import get_session
from compendium.domain.models import AppUser
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.web.csrf import ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PAGE_SIZE = 50


def _render(name: str, request: Request, ctx: dict):
    from compendium.db.engine import get_settings

    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean)
    if fresh:
        set_csrf_cookie(resp, fresh, get_settings().jwt_secret_key)
    return resp


def _filters_qs(params: dict) -> str:
    return urlencode({k: v for k, v in params.items() if v not in (None, "")})


@router.get("/admin/loans")
def admin_loans(
    request: Request,
    due: str | None = Query(default=None),
    branch: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    user: AppUser = Depends(require_web_permission("loan.view.any")),
    session: Session = Depends(get_session),
):
    due_clean = (due or "").strip() or None
    branch_clean = (branch or "").strip() or None
    query_clean = (q or "").strip() or None

    branch_id: int | None = None
    if branch_clean:
        b = SqlBranchRepository(session).get_by_code(branch_clean)
        branch_id = b.id if b is not None else -1

    repo = SqlLoanRepository(session)
    offset = (page - 1) * _PAGE_SIZE
    loans = repo.list_active(
        due=due_clean, branch_id=branch_id, query=query_clean,
        limit=_PAGE_SIZE, offset=offset,
    )
    total = repo.count_active(
        due=due_clean, branch_id=branch_id, query=query_clean,
    )

    has_prev = page > 1
    has_next = offset + len(loans) < total

    filters_qs = _filters_qs({
        "due": due_clean, "branch": branch_clean, "q": query_clean,
    })

    return _render(
        "admin/loans.html",
        request,
        {
            "request": request,
            "user": user,
            "loans": loans,
            "total": total,
            "page": page,
            "has_prev": has_prev,
            "has_next": has_next,
            "filters_qs": filters_qs,
            "due": due_clean or "",
            "branch": branch_clean or "",
            "q": query_clean or "",
            "branches": SqlBranchRepository(session).list(),
            "due_options": [
                ("", "All"),
                ("overdue", "Overdue"),
                ("due_soon", "Due within 3 days"),
                ("on_time", "On-time"),
            ],
        },
    )


@router.get("/admin/fines")
def admin_fines(
    request: Request,
    kind: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    user: AppUser = Depends(require_web_permission("fine.manage")),
    session: Session = Depends(get_session),
):
    kind_clean = (kind or "").strip() or None
    query_clean = (q or "").strip() or None

    repo = SqlFineRepository(session)
    offset = (page - 1) * _PAGE_SIZE
    fines = repo.list_outstanding(
        kind=kind_clean, query=query_clean,
        limit=_PAGE_SIZE, offset=offset,
    )
    total = repo.count_outstanding(kind=kind_clean, query=query_clean)
    grand_total_cents = repo.outstanding_total_all(
        kind=kind_clean, query=query_clean,
    )

    has_prev = page > 1
    has_next = offset + len(fines) < total

    filters_qs = _filters_qs({"kind": kind_clean, "q": query_clean})

    return _render(
        "admin/fines.html",
        request,
        {
            "request": request,
            "user": user,
            "fines": fines,
            "total": total,
            "grand_total_cents": grand_total_cents,
            "page": page,
            "has_prev": has_prev,
            "has_next": has_next,
            "filters_qs": filters_qs,
            "kind": kind_clean or "",
            "q": query_clean or "",
            "kind_options": [
                ("", "All kinds"),
                ("overdue", "Overdue"),
                ("lost", "Lost"),
                ("damaged", "Damaged"),
                ("processing", "Processing"),
                ("other", "Other"),
            ],
        },
    )
