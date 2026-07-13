from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import (
    AmbiguousItemError,
    BusinessRuleError,
    HoldQueueBlockError,
    NotFoundError,
)
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.calendar import CalendarService
from compendium.services.circulation import CirculationService
from compendium.services.site_settings import get_site_setting
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import get_calendar_svc, require_web_permission
from compendium.web.jinja import templates
from compendium.web.routes.scan import permitted_scan_modes

router = APIRouter()

_PERM = "loan.checkout"


def _circ(
    session: Session,
    actor: AppUser | None = None,
    calendar_svc: CalendarService | None = None,
) -> CirculationService:
    settings = get_settings()
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
        work_repo=SqlWorkRepository(session),
    )


def _render(name: str, request: Request, ctx: dict):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean)
    if fresh:
        set_csrf_cookie(resp, fresh)
    return resp


@router.get("/circ")
def circ_desk(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
):
    scan_modes = permitted_scan_modes(user.role.permissions)
    # This is the circulation page: pre-check the circulation modes, leave
    # Catalog available but unchecked. Fall back to all permitted so at least
    # one box is always checked.
    scan_modes_checked = [
        m for m in scan_modes if m in ("checkout", "checkin")
    ] or scan_modes
    return _render(
        "circ/desk.html",
        request,
        {
            "request": request,
            "user": user,
            "scan_modes": scan_modes,
            "scan_modes_checked": scan_modes_checked,
            "scan_isbn_enabled": get_site_setting("circulation_scan_isbn_enabled"),
        },
    )


@router.post("/circ/checkout", response_class=HTMLResponse)
def checkout(
    request: Request,
    barcode: str = Form(),
    card_number: str = Form(),
    override_holds: bool = Form(default=False),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
    calendar_svc: CalendarService = Depends(get_calendar_svc),
):
    check_csrf_form(request, csrf_token)
    try:
        loan = _circ(session, actor=user, calendar_svc=calendar_svc).checkout(
            barcode, card_number, override_holds=override_holds
        )
        due = loan.due_at.strftime("%Y-%m-%d") if loan.due_at else "—"
        return HTMLResponse(
            f"<p class='success-banner'>Checked out <strong>{escape(loan.item.work.title)}</strong> "
            f"(barcode <code>{escape(loan.item.barcode)}</code>) to "
            f"<strong>{escape(loan.patron.full_name)}</strong> "
            f"({escape(loan.patron.library_card_number)}). Due: {escape(due)}</p>"
        )
    except HoldQueueBlockError as exc:
        # Offer the librarian an override button. Re-submits the same form
        # with override_holds=true; the override is audited server-side.
        return HTMLResponse(
            f"<div class='warning-banner'>"
            f"<p>{escape(str(exc))}</p>"
            f"<form method='post' action='/ui/circ/checkout' hx-post='/ui/circ/checkout' hx-target='#circ-result' hx-swap='innerHTML'>"
            f"<input type='hidden' name='barcode' value='{escape(barcode)}'>"
            f"<input type='hidden' name='card_number' value='{escape(card_number)}'>"
            f"<input type='hidden' name='override_holds' value='true'>"
            f"<input type='hidden' name='csrf_token' value='{escape(csrf_token)}'>"
            f"<button type='submit' class='outline'>Override hold queue and check out anyway</button>"
            f"</form></div>"
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<p class='error-banner'>{escape(str(exc))}</p>")


@router.post("/circ/checkin", response_class=HTMLResponse)
def checkin(
    request: Request,
    barcode: str = Form(),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
    calendar_svc: CalendarService = Depends(get_calendar_svc),
):
    check_csrf_form(request, csrf_token)
    try:
        _circ(session, calendar_svc=calendar_svc).checkin(barcode)
        return HTMLResponse(
            f"<p class='success-banner'>Checked in <strong>{escape(barcode)}</strong>.</p>"
        )
    except AmbiguousItemError as exc:
        rows = "".join(
            "<form method='post' action='/ui/circ/checkin' hx-post='/ui/circ/checkin' "
            "hx-target='#circ-result' hx-swap='innerHTML'>"
            f"<input type='hidden' name='barcode' value='{escape(loan.item.barcode)}'>"
            f"<input type='hidden' name='csrf_token' value='{escape(csrf_token)}'>"
            "<button type='submit' class='outline' style='width:auto'>"
            f"{escape(loan.patron.full_name or loan.patron.library_card_number)} "
            f"({escape(loan.patron.library_card_number)}) "
            f"— due {loan.due_at.strftime('%Y-%m-%d')} — copy {escape(loan.item.accession_number)}"
            "</button></form>"
            for loan in exc.loans
        )
        return HTMLResponse(
            f"<div class='warning-banner'><p>{escape(str(exc))}</p>"
            f"<p>Which copy came back?</p>{rows}</div>"
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<p class='error-banner'>{escape(str(exc))}</p>")


@router.get("/admin/claims")
def admin_claims(
    request: Request,
    user: AppUser = Depends(require_web_permission("loan.checkin")),
    session: Session = Depends(get_session),
):
    from compendium.domain.enums import ItemStatus
    from compendium.domain.models import Item, Loan, Patron, Work

    rows = (
        session.query(Loan, Item, Work, Patron)
        .join(Item, Loan.item_id == Item.id)
        .join(Work, Item.work_id == Work.id)
        .join(Patron, Loan.patron_id == Patron.id)
        .filter(
            Loan.returned_at.is_(None),
            Item.status == ItemStatus.CLAIMS_RETURNED.value,
        )
        .order_by(Loan.id)
        .all()
    )
    return _render(
        "admin/claims.html",
        request,
        {"request": request, "user": user, "rows": rows},
    )


@router.post("/circ/renew", response_class=HTMLResponse)
def renew(
    request: Request,
    barcode: str = Form(),
    card_number: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
    calendar_svc: CalendarService = Depends(get_calendar_svc),
):
    check_csrf_form(request, csrf_token)
    try:
        loan = _circ(session, calendar_svc=calendar_svc).renew(
            barcode, card_number.strip() or None
        )
        due = loan.due_at.strftime("%Y-%m-%d") if loan.due_at else "—"
        return HTMLResponse(
            f"<p class='success-banner'>Renewed <strong>{escape(barcode)}</strong>. "
            f"New due date: {escape(due)}</p>"
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<p class='error-banner'>{escape(str(exc))}</p>")
