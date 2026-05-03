"""Web UI for fines, lost/damaged item transitions, and patron self-service."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from compendium.services.site_settings import get_site_setting
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import (
    BlockedByFinesError,
    BusinessRuleError,
    NotFoundError,
    ValidationError,
)
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.circulation import CirculationService
from compendium.services.fines import CheckoutStatus, FineService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import get_web_patron, require_web_permission, require_web_user
from compendium.web.jinja import templates

router = APIRouter()


def _fine_svc(session: Session, user: AppUser | None) -> FineService:
    return FineService(
        fine_repo=SqlFineRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        settings=get_settings(),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=user,
        source="web",
    )


def _circulation(session: Session, user: AppUser | None) -> CirculationService:
    settings = get_settings()
    audit = AuditService(SqlAuditLogRepository(session))
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        hold_pickup_days=get_site_setting("hold_pickup_days"),
        fine_svc=_fine_svc(session, user),
        audit_svc=audit,
        actor=user,
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


# ── Librarian patron-fines view ──────────────────────────────────────────────


@router.get("/patrons/{card_number}/fines")
def patron_fines(
    card_number: str,
    request: Request,
    message: str | None = None,
    error: str | None = None,
    user: AppUser = Depends(require_web_permission("fine.manage")),
    session: Session = Depends(get_session),
):
    patron = SqlPatronRepository(session).get_by_card_number(card_number)
    if patron is None:
        return _render(
            "fines/not_found.html",
            request,
            {"request": request, "user": user, "card_number": card_number},
            status_code=404,
        )
    fines_svc = _fine_svc(session, user)
    fines = fines_svc.list(patron_id=patron.id, limit=200)
    outstanding = fines_svc.outstanding_total(patron.id)

    # Show projected overdue totals for active overdue loans that haven't
    # been materialized yet.
    from compendium.repositories.sql.fine_repository import SqlFineRepository as _FR
    from compendium.repositories.sql.loan_repository import SqlLoanRepository as _LR
    projections: list[dict] = []
    for loan in _LR(session).list_active_overdue(patron_id=patron.id):
        existing = _FR(session).get_outstanding_overdue_for_loan(loan.id)
        amount = fines_svc.projected_overdue_fine(loan)
        if existing is None and amount > 0:
            projections.append({"loan": loan, "amount_cents": amount})

    return _render(
        "fines/patron.html",
        request,
        {
            "request": request,
            "user": user,
            "patron": patron,
            "fines": fines,
            "outstanding_cents": outstanding,
            "projections": projections,
            "message": message,
            "error": error,
        },
    )


@router.post("/patrons/{card_number}/fines/assess-overdue")
def patron_assess_overdue(
    card_number: str,
    request: Request,
    csrf_token: str = Form(...),
    user: AppUser = Depends(require_web_permission("fine.manage")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    patron = SqlPatronRepository(session).get_by_card_number(card_number)
    if patron is None:
        return RedirectResponse(
            f"/ui/patrons?error={quote(f'No patron {card_number}')}",
            status_code=303,
        )
    counts = _fine_svc(session, user).assess_overdue_fines(patron_id=patron.id)
    msg = (
        f"Overdue assessed: {counts['created']} created, "
        f"{counts['updated']} updated, {counts['unchanged']} unchanged."
    )
    return RedirectResponse(
        f"/ui/patrons/{card_number}/fines?message={quote(msg)}",
        status_code=303,
    )


@router.post("/fines/{fine_id}/pay")
def pay_fine(
    fine_id: int,
    request: Request,
    patron_card: str = Form(...),
    csrf_token: str = Form(...),
    user: AppUser = Depends(require_web_permission("fine.manage")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _fine_svc(session, user).pay(fine_id)
        msg = f"Fine #{fine_id} marked paid."
        return RedirectResponse(
            f"/ui/patrons/{patron_card}/fines?message={quote(msg)}",
            status_code=303,
        )
    except (NotFoundError, ValidationError) as exc:
        return RedirectResponse(
            f"/ui/patrons/{patron_card}/fines?error={quote(str(exc))}",
            status_code=303,
        )


@router.post("/fines/{fine_id}/waive")
def waive_fine(
    fine_id: int,
    request: Request,
    patron_card: str = Form(...),
    note: str = Form(""),
    csrf_token: str = Form(...),
    user: AppUser = Depends(require_web_permission("fine.manage")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _fine_svc(session, user).waive(fine_id, note)
        msg = f"Fine #{fine_id} waived."
        return RedirectResponse(
            f"/ui/patrons/{patron_card}/fines?message={quote(msg)}",
            status_code=303,
        )
    except (NotFoundError, ValidationError) as exc:
        return RedirectResponse(
            f"/ui/patrons/{patron_card}/fines?error={quote(str(exc))}",
            status_code=303,
        )


# ── Patron self-service ───────────────────────────────────────────────────────


@router.get("/me/fines")
def my_fines(
    request: Request,
    user: AppUser = Depends(require_web_user),
    patron=Depends(get_web_patron),
    session: Session = Depends(get_session),
):
    fines_svc = _fine_svc(session, user)
    fines = fines_svc.list(patron_id=patron.id, limit=200)
    outstanding = fines_svc.outstanding_total(patron.id)
    return _render(
        "me/fines.html",
        request,
        {
            "request": request,
            "user": user,
            "patron": patron,
            "fines": fines,
            "outstanding_cents": outstanding,
        },
    )


# ── Lost / damaged / clear-* flows ───────────────────────────────────────────


@router.get("/items/{barcode}/lost")
def lost_form(
    barcode: str,
    request: Request,
    user: AppUser = Depends(require_web_permission("fine.manage")),
    session: Session = Depends(get_session),
):
    item = SqlItemRepository(session).get_by_barcode(barcode)
    if item is None:
        return _render(
            "fines/not_found.html",
            request,
            {"request": request, "user": user, "card_number": barcode},
            status_code=404,
        )
    policy = SqlLoanPolicyRepository(session).get_for_media_type(
        item.work.media_type_id
    ) or SqlLoanPolicyRepository(session).get_default()
    default_cost = policy.lost_item_default_cents if policy else None
    return _render(
        "fines/declare_lost.html",
        request,
        {
            "request": request,
            "user": user,
            "item": item,
            "default_cost_cents": default_cost,
            "error": None,
        },
    )


@router.post("/items/{barcode}/lost")
def lost_submit(
    barcode: str,
    request: Request,
    replacement_cost_cents: str = Form(""),
    note: str = Form(""),
    csrf_token: str = Form(...),
    user: AppUser = Depends(require_web_permission("fine.manage")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    cost_cents: int | None
    raw = replacement_cost_cents.strip()
    if not raw:
        cost_cents = None
    else:
        try:
            cost_cents = int(raw)
        except ValueError:
            return _render(
                "fines/declare_lost.html",
                request,
                {
                    "request": request,
                    "user": user,
                    "item": SqlItemRepository(session).get_by_barcode(barcode),
                    "default_cost_cents": None,
                    "error": "Replacement cost must be an integer number of cents.",
                },
                status_code=400,
            )
    try:
        _circulation(session, user).declare_lost(
            barcode, replacement_cost_cents=cost_cents, note=note or None
        )
    except (ValidationError, BusinessRuleError, NotFoundError) as exc:
        return _render(
            "fines/declare_lost.html",
            request,
            {
                "request": request,
                "user": user,
                "item": SqlItemRepository(session).get_by_barcode(barcode),
                "default_cost_cents": None,
                "error": str(exc),
            },
            status_code=400,
        )
    msg = f"Item {barcode} declared lost."
    return RedirectResponse(
        f"/ui/items/{barcode}?message={quote(msg)}",
        status_code=303,
    )


@router.get("/items/{barcode}/damaged")
def damaged_form(
    barcode: str,
    request: Request,
    user: AppUser = Depends(require_web_permission("fine.manage")),
    session: Session = Depends(get_session),
):
    item = SqlItemRepository(session).get_by_barcode(barcode)
    if item is None:
        return _render(
            "fines/not_found.html",
            request,
            {"request": request, "user": user, "card_number": barcode},
            status_code=404,
        )
    return _render(
        "fines/mark_damaged.html",
        request,
        {"request": request, "user": user, "item": item, "error": None},
    )


@router.post("/items/{barcode}/damaged")
def damaged_submit(
    barcode: str,
    request: Request,
    amount_cents: str = Form(""),
    note: str = Form(""),
    csrf_token: str = Form(...),
    user: AppUser = Depends(require_web_permission("fine.manage")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        amt = int(amount_cents)
    except ValueError:
        return _render(
            "fines/mark_damaged.html",
            request,
            {
                "request": request,
                "user": user,
                "item": SqlItemRepository(session).get_by_barcode(barcode),
                "error": "amount_cents must be an integer",
            },
            status_code=400,
        )
    try:
        _circulation(session, user).mark_damaged(barcode, amount_cents=amt, note=note)
    except (ValidationError, BusinessRuleError, NotFoundError) as exc:
        return _render(
            "fines/mark_damaged.html",
            request,
            {
                "request": request,
                "user": user,
                "item": SqlItemRepository(session).get_by_barcode(barcode),
                "error": str(exc),
            },
            status_code=400,
        )
    msg = f"Item {barcode} marked damaged."
    return RedirectResponse(
        f"/ui/items/{barcode}?message={quote(msg)}",
        status_code=303,
    )


@router.post("/items/{barcode}/clear-damage")
def clear_damage(
    barcode: str,
    request: Request,
    csrf_token: str = Form(...),
    user: AppUser = Depends(require_web_permission("fine.manage")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _circulation(session, user).clear_damage(barcode)
        msg = f"Item {barcode} damage cleared."
        return RedirectResponse(
            f"/ui/items/{barcode}?message={quote(msg)}",
            status_code=303,
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/items/{barcode}?error={quote(str(exc))}",
            status_code=303,
        )


@router.post("/items/{barcode}/clear-lost")
def clear_lost(
    barcode: str,
    request: Request,
    csrf_token: str = Form(...),
    user: AppUser = Depends(require_web_permission("fine.manage")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _circulation(session, user).clear_lost(barcode)
        msg = f"Item {barcode} recovered."
        return RedirectResponse(
            f"/ui/items/{barcode}?message={quote(msg)}",
            status_code=303,
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/items/{barcode}?error={quote(str(exc))}",
            status_code=303,
        )


# ── Claims-returned resolutions ─────────────────────────────────────────────


@router.get("/items/{barcode}/verify-returned-confirm")
def verify_returned_confirm_form(
    barcode: str,
    request: Request,
    user: AppUser = Depends(require_web_permission("loan.checkin")),
    session: Session = Depends(get_session),
):
    item = SqlItemRepository(session).get_by_barcode(barcode)
    if item is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"Item '{barcode}' not found"},
            status_code=404,
        )
    return _render(
        "fines/verify_returned_confirm.html",
        request,
        {"request": request, "user": user, "item": item},
    )


@router.post("/items/{barcode}/verify-returned")
def verify_returned(
    barcode: str,
    request: Request,
    csrf_token: str = Form(...),
    user: AppUser = Depends(require_web_permission("loan.checkin")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _circulation(session, user).verify_returned(barcode)
        return RedirectResponse(
            f"/ui/items/{barcode}?message={quote('Verified returned; loan closed.')}",
            status_code=303,
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/items/{barcode}?error={quote(str(exc))}",
            status_code=303,
        )


@router.get("/items/{barcode}/write-off-claim")
def write_off_claim_form(
    barcode: str,
    request: Request,
    user: AppUser = Depends(require_web_permission("loan.checkin")),
    session: Session = Depends(get_session),
):
    from compendium.repositories.sql.item_repository import SqlItemRepository

    item = SqlItemRepository(session).get_by_barcode(barcode)
    if item is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"Item '{barcode}' not found"},
            status_code=404,
        )
    return _render(
        "fines/write_off_claim.html",
        request,
        {"request": request, "user": user, "item": item},
    )


@router.post("/items/{barcode}/write-off-claim")
def write_off_claim(
    barcode: str,
    request: Request,
    note: str = Form(""),
    csrf_token: str = Form(...),
    user: AppUser = Depends(require_web_permission("loan.checkin")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _circulation(session, user).write_off_claim(barcode, note=note)
        return RedirectResponse(
            f"/ui/items/{barcode}?message={quote('Claim written off; loan closed.')}",
            status_code=303,
        )
    except (BusinessRuleError, ValidationError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/items/{barcode}?error={quote(str(exc))}",
            status_code=303,
        )
