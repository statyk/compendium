from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import (
    BusinessRuleError,
    NotFoundError,
    ValidationError,
)
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.counters import SqlCounterRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()


def _catalog_svc(session: Session, actor: AppUser) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="web",
        counter_repo=SqlCounterRepository(session),
    )


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean, status_code=status_code)
    if fresh:
        set_csrf_cookie(resp, fresh, get_settings().jwt_secret_key)
    return resp


@router.get("/creators/{creator_id:int}/edit")
def creator_edit_form(
    creator_id: int,
    request: Request,
    return_to: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("work.edit")),
    session: Session = Depends(get_session),
):
    creator = SqlCreatorRepository(session).get(creator_id)
    if creator is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": "Creator not found"},
            status_code=404,
        )
    works = SqlCreatorRepository(session).list_works(creator_id, include_withdrawn_only=True)
    return _render(
        "creators/edit.html",
        request,
        {
            "request": request,
            "user": user,
            "creator": creator,
            "work_count": len(works),
            "return_to": return_to,
            "error": None,
        },
    )


@router.post("/creators/{creator_id:int}/edit")
def creator_edit_submit(
    creator_id: int,
    request: Request,
    display_name: str = Form(default=""),
    csrf_token: str = Form(default=""),
    return_to: str | None = Form(default=None),
    user: AppUser = Depends(require_web_permission("work.edit")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _catalog_svc(session, user).update_creator(
            creator_id, display_name=display_name
        )
    except NotFoundError:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": "Creator not found"},
            status_code=404,
        )
    except (BusinessRuleError, ValidationError) as exc:
        creator = SqlCreatorRepository(session).get(creator_id)
        works = SqlCreatorRepository(session).list_works(creator_id, include_withdrawn_only=True) if creator else []
        return _render(
            "creators/edit.html",
            request,
            {
                "request": request,
                "user": user,
                "creator": creator,
                "work_count": len(works),
                "return_to": return_to,
                "error": str(exc),
            },
        )
    target = return_to or "/ui/catalog"
    return RedirectResponse(target, status_code=303)
