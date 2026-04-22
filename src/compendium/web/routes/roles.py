from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, ConflictError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.services.audit import AuditService
from compendium.services.roles import RoleService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "role.manage"

PERMISSION_GROUPS = [
    ("Catalog", ["work.view", "work.edit", "item.view", "item.create", "item.edit", "item.delete", "catalog.import"]),
    ("Loans", ["loan.checkout", "loan.checkin", "loan.renew.any", "loan.renew.self", "loan.view.self"]),
    ("Holds", ["hold.place.self", "hold.place.any", "hold.view.self"]),
    ("Administration", ["patron.manage", "user.manage", "role.manage", "policy.edit", "branch.edit"]),
]


def _role_svc(session: Session, actor: AppUser) -> RoleService:
    return RoleService(
        role_repo=SqlRoleRepository(session),
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


@router.get("/roles")
def role_list(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    roles = _role_svc(session, user).list()
    return _render(
        "roles/list.html",
        request,
        {"request": request, "user": user, "roles": roles},
    )


@router.get("/roles/new")
def role_new_form(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    return _render(
        "roles/new.html",
        request,
        {
            "request": request,
            "user": user,
            "permission_groups": PERMISSION_GROUPS,
            "error": None,
        },
    )


@router.post("/roles/new")
def role_create(
    request: Request,
    name: str = Form(),
    full_access: str = Form(default=""),
    permissions: list[str] = Form(default=[]),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    perms = ["*"] if full_access == "on" else permissions
    try:
        role = _role_svc(session, user).create(name=name.strip(), permissions=perms)
    except (BusinessRuleError, ConflictError) as exc:
        return _render(
            "roles/new.html",
            request,
            {
                "request": request,
                "user": user,
                "permission_groups": PERMISSION_GROUPS,
                "error": str(exc),
            },
        )
    return RedirectResponse(f"/ui/roles/{role.id}", status_code=303)


@router.get("/roles/{role_id}")
def role_detail(
    role_id: int,
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    role = _role_svc(session, user).get(role_id)
    if role is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"Role #{role_id} not found"},
            status_code=404,
        )
    return _render(
        "roles/detail.html",
        request,
        {
            "request": request,
            "user": user,
            "role": role,
            "permission_groups": PERMISSION_GROUPS,
            "message": message,
            "error": error,
        },
    )


@router.post("/roles/{role_id}/update")
def role_update(
    role_id: int,
    request: Request,
    name: str = Form(),
    full_access: str = Form(default=""),
    permissions: list[str] = Form(default=[]),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    perms = ["*"] if full_access == "on" else permissions
    try:
        _role_svc(session, user).update(role_id, name=name.strip() or None, permissions=perms)
        return RedirectResponse(f"/ui/roles/{role_id}?message=Role+updated.", status_code=303)
    except (BusinessRuleError, ConflictError, NotFoundError) as exc:
        return RedirectResponse(f"/ui/roles/{role_id}?error={quote(str(exc))}", status_code=303)


@router.post("/roles/{role_id}/clone")
def role_clone(
    role_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    svc = _role_svc(session, user)
    source = svc.get(role_id)
    if source is None:
        return RedirectResponse("/ui/roles?error=Role+not+found.", status_code=303)
    new_name = f"{source.name} (copy)"
    try:
        new_role = svc.clone(role_id, new_name=new_name)
        return RedirectResponse(f"/ui/roles/{new_role.id}", status_code=303)
    except (BusinessRuleError, ConflictError, NotFoundError) as exc:
        return RedirectResponse(f"/ui/roles/{role_id}?error={quote(str(exc))}", status_code=303)
