from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.household_repository import SqlHouseholdRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.households import HouseholdService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "household.manage"


def _svc(session: Session, actor: AppUser) -> HouseholdService:
    return HouseholdService(
        household_repo=SqlHouseholdRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
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


@router.get("/households")
def list_households(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    households = SqlHouseholdRepository(session).list(limit=500)
    patron_repo = SqlPatronRepository(session)
    items = [
        {
            "household": hh,
            "member_count": len(patron_repo.list_by_household(hh.id)),
        }
        for hh in households
    ]
    return _render(
        "households/list.html",
        request,
        {"request": request, "user": user, "items": items},
    )


@router.get("/households/new")
def new_household_form(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    return _render(
        "households/new.html",
        request,
        {"request": request, "user": user, "error": None},
    )


@router.post("/households/new")
def create_household(
    request: Request,
    name: str = Form(),
    notes: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        hh = _svc(session, user).create(name=name.strip(), notes=notes.strip() or None)
    except (BusinessRuleError, ValidationError) as exc:
        return _render(
            "households/new.html",
            request,
            {"request": request, "user": user, "error": str(exc)},
        )
    return RedirectResponse(f"/ui/households/{hh.id}", status_code=303)


@router.get("/households/{household_id}")
def household_detail(
    household_id: int,
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    hh = SqlHouseholdRepository(session).get(household_id)
    if hh is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"Household {household_id} not found"},
            status_code=404,
        )
    members = SqlPatronRepository(session).list_by_household(household_id)
    loan_repo = SqlLoanRepository(session)
    hold_repo = SqlHoldRepository(session)
    member_summaries = [
        {
            "patron": m,
            "loan_count": loan_repo.count_for_patron(m.id),
            "hold_count": hold_repo.count_active(patron_id=m.id),
        }
        for m in members
    ]
    return _render(
        "households/detail.html",
        request,
        {
            "request": request,
            "user": user,
            "household": hh,
            "member_summaries": member_summaries,
            "error": None,
        },
    )


@router.post("/households/{household_id}/edit")
def edit_household(
    household_id: int,
    request: Request,
    name: str = Form(),
    notes: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _svc(session, user).update(
            household_id,
            name=name.strip(),
            notes=notes.strip() or None,
        )
    except (BusinessRuleError, NotFoundError, ValidationError) as exc:
        return RedirectResponse(
            f"/ui/households/{household_id}?error={exc}", status_code=303
        )
    return RedirectResponse(f"/ui/households/{household_id}", status_code=303)


@router.post("/households/{household_id}/delete")
def delete_household(
    household_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _svc(session, user).delete(household_id)
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/households/{household_id}?error={exc}", status_code=303
        )
    return RedirectResponse("/ui/households", status_code=303)


@router.post("/households/{household_id}/members/add")
def add_member(
    household_id: int,
    request: Request,
    card_number: str = Form(),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _svc(session, user).add_member(household_id, card_number.strip())
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/households/{household_id}?error={exc}", status_code=303
        )
    return RedirectResponse(f"/ui/households/{household_id}", status_code=303)


@router.post("/households/{household_id}/members/{card}/remove")
def remove_member(
    household_id: int,
    card: str,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _svc(session, user).remove_member(household_id, card)
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/households/{household_id}?error={exc}", status_code=303
        )
    return RedirectResponse(f"/ui/households/{household_id}", status_code=303)
