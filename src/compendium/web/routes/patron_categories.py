"""Web UI for patron category admin."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.patron_category_repository import (
    SqlPatronCategoryRepository,
)
from compendium.services.audit import AuditService
from compendium.services.patron_categories import PatronCategoryService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "patron.manage"


def _svc(session: Session, actor: AppUser) -> PatronCategoryService:
    return PatronCategoryService(
        repo=SqlPatronCategoryRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="web",
    )


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean, status_code=status_code)
    if fresh:
        set_csrf_cookie(resp, fresh)
    return resp


@router.get("/admin/patron-categories")
def patron_category_list(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    repo = SqlPatronCategoryRepository(session)
    cats = repo.list()
    counts = {c.id: repo.count_patrons_in(c.id) for c in cats}
    policy_counts = {c.id: repo.count_policies_in(c.id) for c in cats}
    return _render(
        "admin/patron_categories.html",
        request,
        {
            "request": request,
            "user": user,
            "categories": cats,
            "counts": counts,
            "policy_counts": policy_counts,
            "message": message,
            "error": error,
        },
    )


@router.post("/admin/patron-categories/new")
def patron_category_create(
    request: Request,
    code: str = Form(),
    display_name: str = Form(),
    is_default: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _svc(session, user).create(
            code, display_name, is_default=(is_default == "on")
        )
        return RedirectResponse(
            "/ui/admin/patron-categories?message=Category+created.", status_code=303
        )
    except (BusinessRuleError, ValidationError) as exc:
        return RedirectResponse(
            f"/ui/admin/patron-categories?error={quote(str(exc))}", status_code=303
        )


@router.post("/admin/patron-categories/{category_id}/update")
def patron_category_update(
    category_id: int,
    request: Request,
    display_name: str = Form(default=""),
    is_default: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _svc(session, user).update(
            category_id,
            display_name=display_name or None,
            is_default=True if is_default == "on" else None,
        )
        return RedirectResponse(
            "/ui/admin/patron-categories?message=Category+updated.", status_code=303
        )
    except (NotFoundError, BusinessRuleError, ValidationError) as exc:
        return RedirectResponse(
            f"/ui/admin/patron-categories?error={quote(str(exc))}", status_code=303
        )


@router.post("/admin/patron-categories/{category_id}/delete")
def patron_category_delete(
    category_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _svc(session, user).delete(category_id)
        return RedirectResponse(
            "/ui/admin/patron-categories?message=Category+deleted.", status_code=303
        )
    except (NotFoundError, BusinessRuleError) as exc:
        return RedirectResponse(
            f"/ui/admin/patron-categories?error={quote(str(exc))}", status_code=303
        )
