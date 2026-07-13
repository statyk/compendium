from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser, MediaType
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.patron_category_repository import (
    SqlPatronCategoryRepository,
)
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
        set_csrf_cookie(resp, fresh)
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
    categories = SqlPatronCategoryRepository(session).list()
    return _render(
        "policies/list.html",
        request,
        {
            "request": request,
            "user": user,
            "policies": policies,
            "media_types": media_types,
            "categories": categories,
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
    categories = SqlPatronCategoryRepository(session).list()
    return _render(
        "policies/new.html",
        request,
        {
            "request": request,
            "user": user,
            "media_types": media_types,
            "categories": categories,
            "error": None,
        },
    )


@router.post("/policies/new")
def policy_create(
    request: Request,
    name: str = Form(),
    loan_period_days: int = Form(),
    max_renewals: int = Form(default=2),
    media_type_id: str = Form(default=""),
    patron_category_id: str = Form(default=""),
    is_default: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    mt_id = int(media_type_id) if media_type_id.strip().isdigit() else None
    cat_id = int(patron_category_id) if patron_category_id.strip().isdigit() else None
    default_flag = is_default == "on"
    try:
        _policy_svc(session, user).create(
            name=name.strip(),
            loan_period_days=loan_period_days,
            max_renewals=max_renewals,
            media_type_id=mt_id,
            patron_category_id=cat_id,
            is_default=default_flag,
        )
    except (BusinessRuleError, NotFoundError) as exc:
        media_types = session.query(MediaType).order_by(MediaType.display_name).all()
        categories = SqlPatronCategoryRepository(session).list()
        return _render(
            "policies/new.html",
            request,
            {
                "request": request,
                "user": user,
                "media_types": media_types,
                "categories": categories,
                "error": str(exc),
            },
        )
    return RedirectResponse("/ui/policies?message=Policy+created.", status_code=303)


@router.post("/policies/{policy_id}/update")
def policy_update(
    policy_id: int,
    request: Request,
    loan_period_days: int = Form(),
    max_renewals: int = Form(),
    is_default: str = Form(default=""),
    patron_category_id: str = Form(default=""),
    overdue_fine_per_day_cents: str = Form(default=""),
    overdue_fine_cap_cents: str = Form(default=""),
    grace_period_days: str = Form(default=""),
    lost_item_default_cents: str = Form(default=""),
    lost_item_processing_fee_cents: str = Form(default=""),
    csrf_token: str = Form(default=""),
    confirm_default: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    from compendium.services.policies import _MISSING

    check_csrf_form(request, csrf_token)
    default_flag: bool | None = True if is_default == "on" else False
    cat_arg: object = (
        int(patron_category_id) if patron_category_id.strip().isdigit() else None
    )

    if default_flag:
        current = SqlLoanPolicyRepository(session).get(policy_id)
        current_default = SqlLoanPolicyRepository(session).get_default()
        needs_confirm = (
            current is not None
            and not current.is_default
            and current_default is not None
            and confirm_default != "1"
        )
        if needs_confirm:
            resubmit = {
                "loan_period_days": loan_period_days,
                "max_renewals": max_renewals,
                "is_default": "on",
                "patron_category_id": patron_category_id,
                "overdue_fine_per_day_cents": overdue_fine_per_day_cents,
                "overdue_fine_cap_cents": overdue_fine_cap_cents,
                "grace_period_days": grace_period_days,
                "lost_item_default_cents": lost_item_default_cents,
                "lost_item_processing_fee_cents": lost_item_processing_fee_cents,
            }
            return _render(
                "policies/default_confirm.html",
                request,
                {
                    "request": request,
                    "user": user,
                    "policy": current,
                    "old_default": current_default,
                    "fields": resubmit,
                },
            )

    def _int_or_missing(raw: str):
        s = raw.strip()
        if not s:
            return None  # empty → clear
        try:
            return int(s)
        except ValueError:
            return _MISSING  # skip on parse error

    def _int_or_none(raw: str):
        # Same as above but returns None for empty (actual clear)
        return _int_or_missing(raw)

    try:
        _policy_svc(session, user).update(
            policy_id,
            loan_period_days=loan_period_days,
            max_renewals=max_renewals,
            is_default=default_flag,
            patron_category_id=cat_arg,
            overdue_fine_per_day_cents=_int_or_none(overdue_fine_per_day_cents),
            overdue_fine_cap_cents=_int_or_none(overdue_fine_cap_cents),
            grace_period_days=int(grace_period_days) if grace_period_days.strip() else None,
            lost_item_default_cents=_int_or_none(lost_item_default_cents),
            lost_item_processing_fee_cents=_int_or_none(lost_item_processing_fee_cents),
        )
        return RedirectResponse("/ui/policies?message=Policy+updated.", status_code=303)
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(f"/ui/policies?error={quote(str(exc))}", status_code=303)


@router.get("/policies/{policy_id}/delete-confirm")
def policy_delete_confirm(
    policy_id: int,
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    policy = SqlLoanPolicyRepository(session).get(policy_id)
    if policy is None:
        return RedirectResponse("/ui/policies?error=Policy+not+found.", status_code=303)
    return _render(
        "policies/delete_confirm.html",
        request,
        {"request": request, "user": user, "policy": policy},
    )


@router.post("/policies/{policy_id}/delete")
def policy_delete(
    policy_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _policy_svc(session, user).delete(policy_id)
        return RedirectResponse("/ui/policies?message=Policy+deleted.", status_code=303)
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(f"/ui/policies?error={quote(str(exc))}", status_code=303)
