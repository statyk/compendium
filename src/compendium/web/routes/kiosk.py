"""Self-checkout kiosk mode.

Scoped UI for an unattended terminal: card scan → item scans → done.
Checkout only (no check-in, no renew, no overrides). Friendly patron-facing
error messages. Client-side idle timeout redirects back to the landing page
so one patron's session doesn't leak into the next.

Auth model: the kiosk device runs as a dedicated user (typically a custom
"Kiosk" role scoped to just `loan.checkout`). Each patron identifies only
by card number; no password prompt.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from compendium.config.settings import Settings
from compendium.services.site_settings import get_site_setting
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import (
    BlockedByFinesError,
    BusinessRuleError,
    HoldQueueBlockError,
    NotFoundError,
)
from compendium.domain.models import AppUser, Patron
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.failed_login_repository import SqlFailedLoginRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.calendar import CalendarService
from compendium.services.circulation import CirculationService
from compendium.services.fines import CheckoutStatus, FineService
from compendium.services.rate_limit import RateLimitService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import get_calendar_svc, require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "loan.checkout"


def _fine_svc(
    session: Session,
    settings: Settings,
    calendar_svc: CalendarService | None = None,
) -> FineService:
    return FineService(
        fine_repo=SqlFineRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        settings=settings,
        calendar_svc=calendar_svc,
        source="kiosk",
    )


def _circ(
    session: Session,
    actor: AppUser,
    settings: Settings,
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
        fine_svc=_fine_svc(session, settings, calendar_svc),
        calendar_svc=calendar_svc,
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="kiosk",
        item_note_repo=SqlItemNoteRepository(session),
    )


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean, status_code=status_code)
    if fresh:
        set_csrf_cookie(resp, fresh)
    return resp


def _gate_patron(session: Session, patron: Patron, settings: Settings) -> str | None:
    """Return a patron-facing error string if this patron can't use the kiosk, else None."""
    if not patron.is_active:
        return "This card is not active. Please see the desk."
    if patron.expires_at is not None and patron.expires_at < date.today():
        return "This card is not active. Please see the desk."
    # CirculationService.checkout treats any non-OK status as a block, so the
    # kiosk gate mirrors that behavior (no silent surprise at scan time).
    status = _fine_svc(session, settings).checkout_status(patron)
    if status != CheckoutStatus.OK:
        return "Your account has outstanding fees. Please see the desk."
    return None


@router.get("/kiosk")
def kiosk_landing(
    request: Request,
    error: str | None = None,
    user: AppUser = Depends(require_web_permission(_PERM)),
):
    settings = get_settings()
    return _render(
        "kiosk/landing.html",
        request,
        {
            "request": request,
            "user": user,
            "error": error,
            "idle_timeout_seconds": get_site_setting("kiosk_idle_timeout_seconds"),
        },
    )


@router.post("/kiosk/start")
def kiosk_start(
    request: Request,
    card_number: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    card = card_number.strip()
    if not card:
        return RedirectResponse(
            "/ui/kiosk?error=" + quote("Please enter or scan a card number."),
            status_code=303,
        )
    rl = RateLimitService(SqlFailedLoginRepository(session))
    retry_after = rl.check("kiosk_card", card)
    if retry_after is not None:
        return RedirectResponse(
            "/ui/kiosk?error=" + quote(
                f"Too many failed attempts. Try again in {retry_after} seconds."
            ),
            status_code=303,
        )
    patron = SqlPatronRepository(session).get_by_card_number(card)
    if patron is None:
        rl.record_failure("kiosk_card", card)
        return RedirectResponse(
            "/ui/kiosk?error=" + quote("Card not recognized. Please see the desk."),
            status_code=303,
        )
    settings = get_settings()
    gate = _gate_patron(session, patron, settings)
    if gate is not None:
        return RedirectResponse("/ui/kiosk?error=" + quote(gate), status_code=303)
    rl.clear("kiosk_card", card)
    return RedirectResponse(
        f"/ui/kiosk/session/{patron.library_card_number}", status_code=303
    )


@router.get("/kiosk/session/{card_number}")
def kiosk_session(
    card_number: str,
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    patron = SqlPatronRepository(session).get_by_card_number(card_number)
    if patron is None:
        return RedirectResponse(
            "/ui/kiosk?error=" + quote("Card not recognized. Please see the desk."),
            status_code=303,
        )
    gate = _gate_patron(session, patron, settings)
    if gate is not None:
        return RedirectResponse("/ui/kiosk?error=" + quote(gate), status_code=303)
    return _render(
        "kiosk/session.html",
        request,
        {
            "request": request,
            "user": user,
            "patron": patron,
            "idle_timeout_seconds": get_site_setting("kiosk_idle_timeout_seconds"),
        },
    )


def _kiosk_error(msg: str) -> str:
    return f'<p class="error-banner">{escape(msg)}</p>'


def _kiosk_success(title: str, due: str, list_id: str = "checkout-list") -> str:
    # Primary swap: the success/status banner.
    # OOB swap: append an <li> to the running list.
    safe_title = escape(title)
    safe_due = escape(due)
    return (
        f'<p class="success-banner">Checked out: {safe_title} — Due {safe_due}</p>'
        f'<li hx-swap-oob="beforeend:#{list_id}">{safe_title} — Due {safe_due}</li>'
    )


@router.post("/kiosk/session/{card_number}/checkout", response_class=HTMLResponse)
def kiosk_checkout(
    card_number: str,
    request: Request,
    barcode: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
    calendar_svc: CalendarService = Depends(get_calendar_svc),
):
    check_csrf_form(request, csrf_token)
    barcode = barcode.strip()
    if not barcode:
        return HTMLResponse(_kiosk_error("Please scan an item barcode."))
    settings = get_settings()
    try:
        loan = _circ(session, user, settings, calendar_svc).checkout(barcode, card_number)
    except NotFoundError:
        return HTMLResponse(_kiosk_error("Item not found. Please try again."))
    except BlockedByFinesError:
        return HTMLResponse(
            _kiosk_error("Your account has outstanding fees. Please see the desk.")
        )
    except HoldQueueBlockError:
        return HTMLResponse(
            _kiosk_error("This item is reserved for another patron. Please see the desk.")
        )
    except BusinessRuleError as exc:
        # Translate the domain error text to patron-friendly language where possible.
        msg = str(exc).lower()
        if "expired" in msg or "not active" in msg:
            friendly = "This card is not active. Please see the desk."
        elif "not loanable" in msg or "non-circulating" in msg:
            friendly = "This item can't be checked out. Please see the desk."
        elif "reserved for another" in msg:
            friendly = "This item is reserved for another patron. Please see the desk."
        elif "not available" in msg:
            friendly = "This item is currently checked out."
        else:
            friendly = "Sorry, this item couldn't be checked out. Please see the desk."
        return HTMLResponse(_kiosk_error(friendly))
    title = loan.item.work.title if loan.item and loan.item.work else "Item"
    due = loan.due_at.strftime("%Y-%m-%d") if loan.due_at else "—"
    return HTMLResponse(_kiosk_success(title, due))
