from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, ConflictError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.audit import AuditService
from compendium.services.auth import AuthService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "user.manage"


def _auth_svc(session: Session, actor: AppUser) -> AuthService:
    return AuthService(
        user_repo=SqlUserRepository(session),
        role_repo=SqlRoleRepository(session),
        settings=get_settings(),
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


@router.get("/users")
def user_list(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    users = SqlUserRepository(session).list(limit=200)
    return _render(
        "users/list.html",
        request,
        {"request": request, "user": user, "users": users},
    )


@router.get("/users/new")
def user_new_form(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    roles = SqlRoleRepository(session).list()
    return _render(
        "users/new.html",
        request,
        {"request": request, "user": user, "roles": roles, "error": None},
    )


@router.post("/users/new")
def user_create(
    request: Request,
    username: str = Form(),
    password: str = Form(),
    email: str = Form(default=""),
    role_name: str = Form(),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        new_user = _auth_svc(session, user).create_user(
            username=username.strip(),
            password=password,
            role_name=role_name,
            email=email.strip() or None,
        )
    except (ConflictError, NotFoundError, BusinessRuleError) as exc:
        roles = SqlRoleRepository(session).list()
        return _render(
            "users/new.html",
            request,
            {"request": request, "user": user, "roles": roles, "error": str(exc)},
        )
    return RedirectResponse(f"/ui/users/{new_user.username}", status_code=303)


@router.get("/users/{username}")
def user_detail(
    username: str,
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    target = SqlUserRepository(session).get_by_username(username)
    if target is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"User '{username}' not found"},
            status_code=404,
        )
    roles = SqlRoleRepository(session).list()
    patron = SqlPatronRepository(session).get_by_user_id(target.id)
    return _render(
        "users/detail.html",
        request,
        {
            "request": request,
            "user": user,
            "target": target,
            "roles": roles,
            "patron": patron,
            "message": message,
            "error": error,
        },
    )


@router.post("/users/{username}/change-role", response_class=HTMLResponse)
def user_change_role(
    username: str,
    request: Request,
    role_name: str = Form(),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _auth_svc(session, user).update_role(username, role_name)
        return RedirectResponse(
            f"/ui/users/{username}?message=Role+updated.", status_code=303
        )
    except (NotFoundError, BusinessRuleError) as exc:
        return RedirectResponse(
            f"/ui/users/{username}?error={exc}", status_code=303
        )


@router.post("/users/{username}/deactivate", response_class=HTMLResponse)
def user_deactivate(
    username: str,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _auth_svc(session, user).deactivate_user(username)
        return HTMLResponse("<span class='error-banner'>User deactivated.</span>")
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<span class='error-banner'>{exc}</span>")
