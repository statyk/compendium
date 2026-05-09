from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import AuthError, BusinessRuleError, ConflictError, NotFoundError, ValidationError
from compendium.domain.models import AppUser, Patron
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_category_repository import SqlPatronCategoryRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.audit import AuditService
from compendium.services.auth import AuthService, assignable_roles, has_permission
from compendium.services.patrons import PatronService
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


def _patron_svc_with_auth(session: Session, actor: AppUser) -> PatronService:
    auth_svc = _auth_svc(session, actor)
    return PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="web",
        auth_svc=auth_svc,
    )


def _parse_date_or_none(s: str):
    s = s.strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError("Date must be YYYY-MM-DD")


def _unlinked_patrons(session: Session) -> list[Patron]:
    """Active patrons with no linked user account."""
    return (
        session.query(Patron)
        .filter(Patron.user_id.is_(None), Patron.is_active == True)  # noqa: E712
        .order_by(Patron.full_name)
        .all()
    )


def _assignable_roles(actor: AppUser, session: Session):
    all_roles = SqlRoleRepository(session).list()
    return assignable_roles(actor.role.permissions, all_roles)


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
    include_inactive: int = Query(default=0),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    show_inactive = include_inactive == 1
    users = SqlUserRepository(session).list(limit=200, include_inactive=show_inactive)
    return _render(
        "users/list.html",
        request,
        {"request": request, "user": user, "users": users, "include_inactive": show_inactive},
    )


@router.get("/users/new")
def user_new_form(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    roles = _assignable_roles(user, session)
    return _render(
        "users/new.html",
        request,
        {
            "request": request,
            "user": user,
            "roles": roles,
            "unlinked_patrons": _unlinked_patrons(session),
            "error": None,
        },
    )


@router.post("/users/new")
def user_create(
    request: Request,
    username: str = Form(),
    password: str = Form(),
    email: str = Form(default=""),
    role_name: str = Form(),
    patron_mode: str = Form(default=""),
    link_patron_card: str = Form(default=""),
    patron_full_name: str = Form(default=""),
    patron_email: str = Form(default=""),
    patron_phone: str = Form(default=""),
    patron_expires: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    allowed = _assignable_roles(user, session)
    allowed_names = {r.name for r in allowed}
    if role_name not in allowed_names:
        roles = allowed
        return _render(
            "users/new.html",
            request,
            {
                "request": request,
                "user": user,
                "roles": roles,
                "unlinked_patrons": _unlinked_patrons(session),
                "error": f"Your account cannot assign the '{role_name}' role.",
            },
            status_code=403,
        )
    try:
        new_user = _auth_svc(session, user).create_user(
            username=username.strip(),
            password=password,
            role_name=role_name,
            email=email.strip() or None,
        )
        if role_name == "Patron":
            if patron_mode == "link" and link_patron_card.strip():
                patron_svc = PatronService(
                    patron_repo=SqlPatronRepository(session),
                    loan_repo=SqlLoanRepository(session),
                    hold_repo=SqlHoldRepository(session),
                    audit_svc=AuditService(SqlAuditLogRepository(session)),
                    actor=user,
                    source="web",
                )
                patron_svc.link_user(link_patron_card.strip(), new_user.id)
            elif patron_mode == "create" and patron_full_name.strip():
                patron_svc = PatronService(
                    patron_repo=SqlPatronRepository(session),
                    loan_repo=SqlLoanRepository(session),
                    hold_repo=SqlHoldRepository(session),
                    audit_svc=AuditService(SqlAuditLogRepository(session)),
                    actor=user,
                    source="web",
                )
                exp_at = _parse_date_or_none(patron_expires)
                patron_svc.create(
                    full_name=patron_full_name.strip(),
                    contact_email=patron_email.strip() or None,
                    contact_phone=patron_phone.strip() or None,
                    user_id=new_user.id,
                    expires_at=exp_at,
                )
    except (ConflictError, NotFoundError, BusinessRuleError, ValidationError) as exc:
        roles = _assignable_roles(user, session)
        return _render(
            "users/new.html",
            request,
            {
                "request": request,
                "user": user,
                "roles": roles,
                "unlinked_patrons": _unlinked_patrons(session),
                "error": str(exc),
            },
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
    roles = _assignable_roles(user, session)
    patron = SqlPatronRepository(session).get_by_user_id(target.id)
    unlinked = _unlinked_patrons(session) if patron is None and target.is_active else []
    return _render(
        "users/detail.html",
        request,
        {
            "request": request,
            "user": user,
            "target": target,
            "roles": roles,
            "patron": patron,
            "unlinked_patrons": unlinked,
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
    allowed_names = {r.name for r in _assignable_roles(user, session)}
    if role_name not in allowed_names:
        return RedirectResponse(
            f"/ui/users/{quote(username)}?error={quote(f'Your account cannot assign the {role_name!r} role.')}",
            status_code=303,
        )
    try:
        _auth_svc(session, user).update_role(username, role_name)
        return RedirectResponse(
            f"/ui/users/{username}?message=Role+updated.", status_code=303
        )
    except (NotFoundError, BusinessRuleError) as exc:
        return RedirectResponse(
            f"/ui/users/{quote(username)}?error={quote(str(exc))}", status_code=303
        )


@router.post("/users/{username}/reset-password")
def user_reset_password(
    username: str,
    request: Request,
    actor_current_password: str = Form(),
    new_password: str = Form(),
    confirm_password: str = Form(),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    if username == user.username:
        return RedirectResponse("/ui/me/password", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse(
            f"/ui/users/{quote(username)}?error={quote('New passwords do not match.')}",
            status_code=303,
        )
    try:
        _auth_svc(session, user).admin_reset_password(
            target_username=username,
            actor_current_password=actor_current_password,
            new_password=new_password,
        )
    except (AuthError, BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/users/{quote(username)}?error={quote(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        f"/ui/users/{quote(username)}?message={quote('Password reset.')}",
        status_code=303,
    )


@router.post("/users/{username}/link-patron")
def user_link_patron(
    username: str,
    request: Request,
    card_number: str = Form(),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    target = SqlUserRepository(session).get_by_username(username)
    if target is None:
        return RedirectResponse(f"/ui/users?error=User+not+found.", status_code=303)
    try:
        patron_svc = PatronService(
            patron_repo=SqlPatronRepository(session),
            loan_repo=SqlLoanRepository(session),
            hold_repo=SqlHoldRepository(session),
            audit_svc=AuditService(SqlAuditLogRepository(session)),
            actor=user,
            source="web",
        )
        patron_svc.link_user(card_number.strip(), target.id)
        return RedirectResponse(
            f"/ui/users/{quote(username)}?message=Patron+record+linked.", status_code=303
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/users/{quote(username)}?error={quote(str(exc))}", status_code=303
        )


@router.post("/users/{username}/unlink-patron")
def user_unlink_patron(
    username: str,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    target = SqlUserRepository(session).get_by_username(username)
    if target is None:
        return RedirectResponse(f"/ui/users?error=User+not+found.", status_code=303)
    try:
        patron = SqlPatronRepository(session).get_by_user_id(target.id)
        if patron is None:
            raise BusinessRuleError("This user has no linked patron record.")
        patron_svc = PatronService(
            patron_repo=SqlPatronRepository(session),
            loan_repo=SqlLoanRepository(session),
            hold_repo=SqlHoldRepository(session),
            audit_svc=AuditService(SqlAuditLogRepository(session)),
            actor=user,
            source="web",
        )
        patron_svc.unlink_user(patron.library_card_number)
        return RedirectResponse(
            f"/ui/users/{quote(username)}?message=Patron+record+unlinked.", status_code=303
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/users/{quote(username)}?error={quote(str(exc))}", status_code=303
        )


@router.post("/users/{username}/create-patron")
def user_create_patron(
    username: str,
    request: Request,
    full_name: str = Form(),
    contact_email: str = Form(default=""),
    contact_phone: str = Form(default=""),
    expires_at: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    target = SqlUserRepository(session).get_by_username(username)
    if target is None:
        return RedirectResponse(f"/ui/users?error=User+not+found.", status_code=303)
    try:
        exp_at = _parse_date_or_none(expires_at)
        patron_svc = PatronService(
            patron_repo=SqlPatronRepository(session),
            loan_repo=SqlLoanRepository(session),
            hold_repo=SqlHoldRepository(session),
            audit_svc=AuditService(SqlAuditLogRepository(session)),
            actor=user,
            source="web",
        )
        patron_svc.create(
            full_name=full_name.strip(),
            contact_email=contact_email.strip() or None,
            contact_phone=contact_phone.strip() or None,
            user_id=target.id,
            expires_at=exp_at,
        )
        return RedirectResponse(
            f"/ui/users/{quote(username)}?message=Patron+record+created+and+linked.", status_code=303
        )
    except (BusinessRuleError, NotFoundError, ValidationError) as exc:
        return RedirectResponse(
            f"/ui/users/{quote(username)}?error={quote(str(exc))}", status_code=303
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
    if username == user.username:
        return HTMLResponse(
            "<span class='error-banner'>You cannot deactivate your own account.</span>"
        )
    try:
        _auth_svc(session, user).deactivate_user(username)
        return HTMLResponse("<span class='error-banner'>User deactivated.</span>")
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<span class='error-banner'>{escape(str(exc))}</span>")


@router.post("/users/{username}/reactivate")
def user_reactivate(
    username: str,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _auth_svc(session, user).reactivate_user(username)
        return RedirectResponse(
            f"/ui/users/{quote(username)}?message=User+reactivated.", status_code=303
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/users/{quote(username)}?error={quote(str(exc))}", status_code=303
        )
