from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser, Patron
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_category_repository import (
    SqlPatronCategoryRepository,
)
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.holds import HoldService
from compendium.services.patrons import PatronService, _MISSING
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "patron.manage"


def _patron_svc(session: Session, actor: AppUser) -> PatronService:
    return PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
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


def _unlinked_users(session: Session) -> list[AppUser]:
    """Return active users who have no patron record linked."""
    linked_ids = {
        row[0]
        for row in session.query(Patron.user_id).filter(Patron.user_id.isnot(None)).all()
    }
    return [u for u in SqlUserRepository(session).list(limit=500) if u.id not in linked_ids]


@router.get("/patrons")
def patron_list(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    patrons = SqlPatronRepository(session).list()
    return _render(
        "patrons/list.html",
        request,
        {"request": request, "user": user, "patrons": patrons},
    )


def _categories(session: Session):
    return SqlPatronCategoryRepository(session).list()


def _parse_date_or_none(s: str):
    s = s.strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError("Date must be YYYY-MM-DD")


@router.get("/patrons/new")
def patron_new_form(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    return _render(
        "patrons/new.html",
        request,
        {
            "request": request,
            "user": user,
            "error": None,
            "unlinked_users": _unlinked_users(session),
            "categories": _categories(session),
        },
    )


@router.post("/patrons/new")
def patron_create(
    request: Request,
    full_name: str = Form(),
    contact_email: str = Form(default=""),
    contact_phone: str = Form(default=""),
    user_id: str = Form(default=""),
    category_id: str = Form(default=""),
    expires_at: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    linked_user_id: int | None = int(user_id) if user_id.strip() else None
    cat_id: int | None = int(category_id) if category_id.strip() else None
    try:
        exp_at = _parse_date_or_none(expires_at)
        patron = _patron_svc(session, user).create(
            full_name=full_name.strip(),
            contact_email=contact_email.strip() or None,
            contact_phone=contact_phone.strip() or None,
            user_id=linked_user_id,
            category_id=cat_id,
            expires_at=exp_at,
        )
    except (BusinessRuleError, ValidationError) as exc:
        return _render(
            "patrons/new.html",
            request,
            {
                "request": request,
                "user": user,
                "error": str(exc),
                "unlinked_users": _unlinked_users(session),
                "categories": _categories(session),
            },
        )
    return RedirectResponse(f"/ui/patrons/{patron.library_card_number}", status_code=303)


@router.post("/patrons/{card_number}/edit")
def patron_edit(
    card_number: str,
    request: Request,
    category_id: str = Form(default=""),
    expires_at: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        exp_at = _parse_date_or_none(expires_at) if expires_at.strip() else None
        cat_arg: object = int(category_id) if category_id.strip() else None
        _patron_svc(session, user).update(
            card_number, category_id=cat_arg, expires_at=exp_at
        )
    except (BusinessRuleError, NotFoundError, ValidationError) as exc:
        return RedirectResponse(
            f"/ui/patrons/{card_number}?error={exc}", status_code=303
        )
    return RedirectResponse(
        f"/ui/patrons/{card_number}?message=Patron+updated.", status_code=303
    )


@router.get("/patrons/{card_number}")
def patron_detail(
    card_number: str,
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    patron = SqlPatronRepository(session).get_by_card_number(card_number)
    if patron is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"Patron '{card_number}' not found"},
            status_code=404,
        )
    loans = SqlLoanRepository(session).get_active_for_patron(patron.id)
    holds = SqlHoldRepository(session).get_active_for_patron(patron.id)
    linked_user = SqlUserRepository(session).get(patron.user_id) if patron.user_id else None
    avail_users = _unlinked_users(session) if linked_user is None else []
    return _render(
        "patrons/detail.html",
        request,
        {
            "request": request,
            "user": user,
            "patron": patron,
            "loans": loans,
            "holds": holds,
            "linked_user": linked_user,
            "unlinked_users": avail_users,
            "categories": _categories(session),
            "message": message,
            "error": error,
        },
    )


@router.post("/patrons/{card_number}/link-user")
def patron_link_user(
    card_number: str,
    request: Request,
    user_id: int = Form(),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _patron_svc(session, user).link_user(card_number, user_id)
        return RedirectResponse(
            f"/ui/patrons/{card_number}?message=User+account+linked.", status_code=303
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/patrons/{card_number}?error={exc}", status_code=303
        )


@router.post("/patrons/{card_number}/unlink-user")
def patron_unlink_user(
    card_number: str,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _patron_svc(session, user).unlink_user(card_number)
        return RedirectResponse(
            f"/ui/patrons/{card_number}?message=User+account+unlinked.", status_code=303
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/patrons/{card_number}?error={exc}", status_code=303
        )


@router.get("/patrons/{card_number}/loans")
def patron_loans(
    card_number: str,
    request: Request,
    status: str = Query(default="active"),
    page: int = Query(default=1, ge=1),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    patron = SqlPatronRepository(session).get_by_card_number(card_number)
    if patron is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"Patron '{card_number}' not found"},
            status_code=404,
        )
    if status not in ("active", "returned", "all"):
        status = "active"
    page_size = 50
    offset = (page - 1) * page_size
    loan_repo = SqlLoanRepository(session)
    loans = loan_repo.list_for_patron(
        patron.id, status=status, limit=page_size, offset=offset
    )
    total = loan_repo.count_for_patron(patron.id, status=status)
    has_prev = page > 1
    has_next = offset + len(loans) < total
    return _render(
        "patrons/loans.html",
        request,
        {
            "request": request,
            "user": user,
            "patron": patron,
            "loans": loans,
            "status": status,
            "total": total,
            "page": page,
            "has_prev": has_prev,
            "has_next": has_next,
        },
    )


@router.post("/patrons/{card_number}/holds/{hold_id}/cancel", response_class=HTMLResponse)
def patron_cancel_hold(
    card_number: str,
    hold_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission("hold.place.any")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    hold_repo = SqlHoldRepository(session)
    hold = hold_repo.get(hold_id)
    if hold is None:
        return HTMLResponse("<span class='error-banner'>Hold not found.</span>")
    patron = SqlPatronRepository(session).get_by_card_number(card_number)
    if patron is None or hold.patron_id != patron.id:
        return HTMLResponse("<span class='error-banner'>Hold does not belong to this patron.</span>")
    settings = get_settings()
    holds_svc = HoldService(
        hold_repo=hold_repo,
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
        hold_expiry_days=settings.hold_expiry_days,
        hold_pickup_days=settings.hold_pickup_days,
    )
    try:
        holds_svc.cancel(hold_id, hold.patron_id)
        return HTMLResponse(
            f"<tr><td colspan='4'><em>Hold on "
            f"'{escape(hold.work.title)}' cancelled.</em></td></tr>"
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(
            f"<tr><td colspan='4' class='error-banner'>{escape(str(exc))}</td></tr>"
        )


@router.post("/patrons/{card_number}/deactivate", response_class=HTMLResponse)
def deactivate_patron(
    card_number: str,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _patron_svc(session, user).deactivate(card_number)
        return HTMLResponse("<span class='error-banner'>Patron deactivated.</span>")
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<span class='error-banner'>{escape(str(exc))}</span>")
