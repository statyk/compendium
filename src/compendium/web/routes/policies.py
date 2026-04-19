from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser, MediaType
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.services.audit import AuditService
from compendium.services.policies import PolicyService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "policy.edit"


def _policy_svc(session: Session, actor: AppUser) -> PolicyService:
    return PolicyService(
        policy_repo=SqlLoanPolicyRepository(session),
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
        set_csrf_cookie(resp, fresh, get_settings().jwt_secret_key)
    return resp


@router.get("/policies")
def policy_list(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    policies = _policy_svc(session, user).list()
    media_types = session.query(MediaType).order_by(MediaType.display_name).all()
    return _render(
        "policies/list.html",
        request,
        {
            "request": request,
            "user": user,
            "policies": policies,
            "media_types": media_types,
            "message": message,
            "error": error,
        },
    )


@router.get("/policies/new")
def policy_new_form(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    media_types = session.query(MediaType).order_by(MediaType.display_name).all()
    return _render(
        "policies/new.html",
        request,
        {"request": request, "user": user, "media_types": media_types, "error": None},
    )


@router.post("/policies/new")
def policy_create(
    request: Request,
    name: str = Form(),
    loan_period_days: int = Form(),
    max_renewals: int = Form(default=2),
    media_type_id: str = Form(default=""),
    is_default: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    mt_id = int(media_type_id) if media_type_id.strip().isdigit() else None
    default_flag = is_default == "on"
    try:
        _policy_svc(session, user).create(
            name=name.strip(),
            loan_period_days=loan_period_days,
            max_renewals=max_renewals,
            media_type_id=mt_id,
            is_default=default_flag,
        )
    except (BusinessRuleError, NotFoundError) as exc:
        media_types = session.query(MediaType).order_by(MediaType.display_name).all()
        return _render(
            "policies/new.html",
            request,
            {"request": request, "user": user, "media_types": media_types, "error": str(exc)},
        )
    return RedirectResponse("/ui/policies?message=Policy+created.", status_code=303)


@router.post("/policies/{policy_id}/update")
def policy_update(
    policy_id: int,
    request: Request,
    loan_period_days: int = Form(),
    max_renewals: int = Form(),
    is_default: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    default_flag: bool | None = True if is_default == "on" else False
    try:
        _policy_svc(session, user).update(
            policy_id,
            loan_period_days=loan_period_days,
            max_renewals=max_renewals,
            is_default=default_flag,
        )
        return RedirectResponse("/ui/policies?message=Policy+updated.", status_code=303)
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(f"/ui/policies?error={exc}", status_code=303)
