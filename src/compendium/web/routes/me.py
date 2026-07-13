from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.services.site_settings import get_site_setting
from compendium.domain.errors import AuthError, BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser, Patron
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.auth import AuthService
from compendium.services.circulation import CirculationService
from compendium.services.holds import HoldService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.services.calendar import CalendarService
from compendium.web.deps import AUTH_COOKIE, get_calendar_svc, get_web_patron, require_web_user
from compendium.web.jinja import templates

router = APIRouter()


def _circ(
    session: Session,
    actor: AppUser | None = None,
    calendar_svc: CalendarService | None = None,
) -> CirculationService:
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        hold_pickup_days=get_site_setting("hold_pickup_days"),
        calendar_svc=calendar_svc,
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="web",
        item_note_repo=SqlItemNoteRepository(session),
    )


def _holds_svc(
    session: Session,
    actor: AppUser | None = None,
    calendar_svc: CalendarService | None = None,
) -> HoldService:
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
        hold_expiry_days=get_site_setting("hold_expiry_days"),
        hold_pickup_days=get_site_setting("hold_pickup_days"),
        calendar_svc=calendar_svc,
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


@router.get("/me/loans")
def my_loans(
    request: Request,
    user: AppUser = Depends(require_web_user),
    patron: Patron = Depends(get_web_patron),
    session: Session = Depends(get_session),
):
    loans = SqlLoanRepository(session).get_active_for_patron(patron.id)
    return _render(
        "me/loans.html",
        request,
        {"request": request, "user": user, "patron": patron, "loans": loans},
    )


@router.post("/me/loans/{loan_id:int}/renew", response_class=HTMLResponse)
def renew_loan(
    loan_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_user),
    patron: Patron = Depends(get_web_patron),
    session: Session = Depends(get_session),
    calendar_svc: CalendarService = Depends(get_calendar_svc),
):
    check_csrf_form(request, csrf_token)
    try:
        loan = _circ(session, calendar_svc=calendar_svc).renew_by_id(loan_id, patron_id=patron.id)
        return _render(
            "me/_loan_row.html",
            request,
            {"request": request, "user": user, "loan": loan, "notice": "Renewed"},
        )
    except (BusinessRuleError, NotFoundError) as exc:
        loan = SqlLoanRepository(session).get(loan_id)
        if loan is None or loan.patron_id != patron.id:
            return HTMLResponse(
                f"<tr><td colspan='4' class='error-banner'>{escape(str(exc))}</td></tr>"
            )
        return _render(
            "me/_loan_row.html",
            request,
            {"request": request, "user": user, "loan": loan, "error": str(exc)},
        )


@router.post("/me/loans/{loan_id:int}/claim-returned", response_class=HTMLResponse)
def claim_loan_returned(
    loan_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_user),
    patron: Patron = Depends(get_web_patron),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    loan = SqlLoanRepository(session).get(loan_id)
    if loan is None or loan.patron_id != patron.id:
        return HTMLResponse(
            "<tr><td colspan='4' class='error-banner'>Loan not found or not yours.</td></tr>"
        )
    item = SqlItemRepository(session).get(loan.item_id)
    if item is None:
        return HTMLResponse(
            "<tr><td colspan='4' class='error-banner'>Item not found.</td></tr>"
        )
    try:
        _circ(session, actor=user).claim_returned(item.barcode)
        return _render(
            "me/_loan_row.html",
            request,
            {
                "request": request,
                "user": user,
                "loan": loan,
                "notice": "Claim submitted — library will investigate.",
                "hide_actions": True,
            },
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return _render(
            "me/_loan_row.html",
            request,
            {"request": request, "user": user, "loan": loan, "error": str(exc)},
        )


@router.get("/me/holds")
def my_holds(
    request: Request,
    error: str | None = None,
    user: AppUser = Depends(require_web_user),
    patron: Patron = Depends(get_web_patron),
    session: Session = Depends(get_session),
):
    from compendium.repositories.sql.fine_repository import SqlFineRepository
    from compendium.services.fines import CheckoutStatus, FineService

    hold_repo = SqlHoldRepository(session)
    holds = hold_repo.get_active_for_patron(patron.id)
    queue_positions = {h.id: hold_repo.queue_position(h.id) for h in holds}
    fine_svc = FineService(
        fine_repo=SqlFineRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        settings=get_settings(),
    )
    status = fine_svc.checkout_status(patron)
    outstanding = fine_svc.outstanding_total(patron.id)
    return _render(
        "me/holds.html",
        request,
        {
            "request": request,
            "user": user,
            "patron": patron,
            "holds": holds,
            "queue_positions": queue_positions,
            "error": error,
            "pay_at_pickup_warning": status == CheckoutStatus.BLOCKED_AT_PICKUP,
            "outstanding_cents": outstanding,
        },
    )


def _auth_svc(session: Session, actor: AppUser) -> AuthService:
    return AuthService(
        user_repo=SqlUserRepository(session),
        role_repo=SqlRoleRepository(session),
        settings=get_settings(),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="web",
    )


@router.get("/me/password")
def password_form(
    request: Request,
    user: AppUser = Depends(require_web_user),
):
    return _render(
        "me/password.html",
        request,
        {"request": request, "user": user, "error": None},
    )


@router.post("/me/password")
def password_change(
    request: Request,
    current_password: str = Form(),
    new_password: str = Form(),
    confirm_password: str = Form(),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_user),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    if new_password != confirm_password:
        return _render(
            "me/password.html",
            request,
            {"request": request, "user": user, "error": "New passwords do not match."},
            status_code=400,
        )
    try:
        _auth_svc(session, user).change_password(
            user.username, current_password, new_password
        )
    except AuthError as exc:
        return _render(
            "me/password.html",
            request,
            {"request": request, "user": user, "error": str(exc)},
            status_code=401,
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return _render(
            "me/password.html",
            request,
            {"request": request, "user": user, "error": str(exc)},
            status_code=400,
        )
    # Force re-login so the new password is in play. JWTs are stateless, but
    # clearing the cookie gives a clean "log in again" signal without relying
    # on token-side invalidation.
    response = RedirectResponse(
        "/ui/login?message=Password+changed.+Please+log+in+again.",
        status_code=303,
    )
    response.delete_cookie(AUTH_COOKIE)
    return response


@router.post("/me/holds/{hold_id:int}/cancel", response_class=HTMLResponse)
def cancel_hold(
    hold_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_user),
    patron: Patron = Depends(get_web_patron),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _holds_svc(session, actor=user).cancel(hold_id, patron_id=patron.id)
        return HTMLResponse("<tr><td colspan='4'><em>Hold cancelled.</em></td></tr>")
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(
            f"<tr><td colspan='4' class='error-banner'>{escape(str(exc))}</td></tr>"
        )


@router.post("/me/holds/{hold_id:int}/suspend")
def suspend_hold(
    hold_id: int,
    request: Request,
    until: str = Form(default=""),
    reason: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_user),
    patron: Patron = Depends(get_web_patron),
    session: Session = Depends(get_session),
):
    from datetime import datetime

    check_csrf_form(request, csrf_token)
    try:
        parsed = datetime.strptime(until.strip(), "%Y-%m-%d").date()
    except ValueError:
        return RedirectResponse(
            "/ui/me/holds?error=Please+pick+a+valid+end+date.", status_code=303
        )
    try:
        _holds_svc(session, actor=user).suspend(
            hold_id, until=parsed, patron_id=patron.id, reason=reason.strip() or None
        )
        return RedirectResponse("/ui/me/holds", status_code=303)
    except (BusinessRuleError, NotFoundError, ValidationError) as exc:
        from urllib.parse import quote

        return RedirectResponse(
            f"/ui/me/holds?error={quote(str(exc))}", status_code=303
        )


@router.post("/me/holds/{hold_id:int}/resume", response_class=HTMLResponse)
def resume_hold(
    hold_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_user),
    patron: Patron = Depends(get_web_patron),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _holds_svc(session, actor=user).resume(hold_id, patron_id=patron.id)
        return HTMLResponse("<tr><td colspan='4'><em>Hold resumed. Refresh to see updated state.</em></td></tr>")
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(
            f"<tr><td colspan='4' class='error-banner'>{escape(str(exc))}</td></tr>"
        )
