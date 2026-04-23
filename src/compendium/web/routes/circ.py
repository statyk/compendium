from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, HoldQueueBlockError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.circulation import CirculationService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "loan.checkout"


def _circ(session: Session, actor: AppUser | None = None) -> CirculationService:
    settings = get_settings()
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        hold_pickup_days=settings.hold_pickup_days,
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="web",
    )


def _render(name: str, request: Request, ctx: dict):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean)
    if fresh:
        set_csrf_cookie(resp, fresh, get_settings().jwt_secret_key)
    return resp


@router.get("/circ")
def circ_desk(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
):
    return _render("circ/desk.html", request, {"request": request, "user": user})


@router.post("/circ/checkout", response_class=HTMLResponse)
def checkout(
    request: Request,
    barcode: str = Form(),
    card_number: str = Form(),
    override_holds: bool = Form(default=False),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        loan = _circ(session, actor=user).checkout(
            barcode, card_number, override_holds=override_holds
        )
        due = loan.due_at.strftime("%Y-%m-%d") if loan.due_at else "—"
        return HTMLResponse(
            f"<p class='success-banner'>Checked out <strong>{escape(barcode)}</strong> to "
            f"<strong>{escape(card_number)}</strong>. Due: {escape(due)}</p>"
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
):
    check_csrf_form(request, csrf_token)
    try:
        _circ(session).checkin(barcode)
        return HTMLResponse(
            f"<p class='success-banner'>Checked in <strong>{escape(barcode)}</strong>.</p>"
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<p class='error-banner'>{escape(str(exc))}</p>")


@router.post("/circ/renew", response_class=HTMLResponse)
def renew(
    request: Request,
    barcode: str = Form(),
    card_number: str = Form(),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        loan = _circ(session).renew(barcode, card_number)
        due = loan.due_at.strftime("%Y-%m-%d") if loan.due_at else "—"
        return HTMLResponse(
            f"<p class='success-banner'>Renewed <strong>{escape(barcode)}</strong>. "
            f"New due date: {escape(due)}</p>"
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<p class='error-banner'>{escape(str(exc))}</p>")
