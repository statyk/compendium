from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser, Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.circulation import CirculationService
from compendium.services.holds import HoldService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import get_web_patron, require_web_user
from compendium.web.jinja import templates

router = APIRouter()


def _circ(session: Session) -> CirculationService:
    settings = get_settings()
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        hold_pickup_days=settings.hold_pickup_days,
    )


def _holds_svc(session: Session) -> HoldService:
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_expiry_days=get_settings().hold_expiry_days,
    )


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean, status_code=status_code)
    if fresh:
        set_csrf_cookie(resp, fresh, get_settings().jwt_secret_key)
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
):
    check_csrf_form(request, csrf_token)
    try:
        loan = _circ(session).renew_by_id(loan_id, patron_id=patron.id)
        due = loan.due_at.strftime("%Y-%m-%d") if loan.due_at else "—"
        return HTMLResponse(
            f"<td>{loan.item.barcode}</td>"
            f"<td>{loan.item.work.title}</td>"
            f"<td>{due} <small>(renewal {loan.renewal_count})</small></td>"
            f"<td><em>Renewed</em></td>"
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<td colspan='4' class='error-banner'>{exc}</td>")


@router.get("/me/holds")
def my_holds(
    request: Request,
    user: AppUser = Depends(require_web_user),
    patron: Patron = Depends(get_web_patron),
    session: Session = Depends(get_session),
):
    holds = SqlHoldRepository(session).get_active_for_patron(patron.id)
    return _render(
        "me/holds.html",
        request,
        {"request": request, "user": user, "patron": patron, "holds": holds},
    )


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
        _holds_svc(session).cancel(hold_id, patron_id=patron.id)
        return HTMLResponse("<tr><td colspan='4'><em>Hold cancelled.</em></td></tr>")
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<tr><td colspan='4' class='error-banner'>{exc}</td></tr>")
