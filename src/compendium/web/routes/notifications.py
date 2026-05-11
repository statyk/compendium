"""Web UI for notification admin viewer + patron self-service opt-out."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import NotFoundError, ValidationError
from compendium.domain.models import AppUser, Patron
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.notification_repository import SqlNotificationRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.notifications import NotificationService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import get_web_patron, require_web_permission, require_web_user
from compendium.web.jinja import templates

router = APIRouter()


def _notif_svc(session: Session, user: AppUser | None) -> NotificationService:
    return NotificationService(
        notification_repo=SqlNotificationRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        settings=get_settings(),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=user,
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


# ── Admin viewer ─────────────────────────────────────────────────────────────


@router.get("/admin/notifications")
def notifications_list(
    request: Request,
    status: str = Query(default=""),
    template_key: str = Query(default=""),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("notification.manage")),
    session: Session = Depends(get_session),
):
    rows = _notif_svc(session, user).list(
        status=status or None,
        template_key=template_key or None,
        limit=min(limit, 200),
        offset=offset,
    )
    return _render(
        "notifications/list.html",
        request,
        {
            "request": request,
            "user": user,
            "rows": rows,
            "selected_status": status,
            "selected_template": template_key,
            "limit": limit,
            "offset": offset,
            "message": message,
            "error": error,
        },
    )


@router.post("/admin/notifications/{notification_id}/retry")
def notifications_retry(
    notification_id: int,
    request: Request,
    csrf_token: str = Form(...),
    user: AppUser = Depends(require_web_permission("notification.manage")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _notif_svc(session, user).retry(notification_id)
        msg = f"Notification #{notification_id} queued for retry."
        return RedirectResponse(
            f"/ui/admin/notifications?message={quote(msg)}", status_code=303
        )
    except (NotFoundError, ValidationError) as exc:
        return RedirectResponse(
            f"/ui/admin/notifications?error={quote(str(exc))}", status_code=303
        )


# ── /me opt-out toggle ───────────────────────────────────────────────────────


@router.get("/me/preferences")
def my_preferences(
    request: Request,
    user: AppUser = Depends(require_web_user),
    patron: Patron = Depends(get_web_patron),
    message: str | None = Query(default=None),
):
    return _render(
        "me/preferences.html",
        request,
        {
            "request": request,
            "user": user,
            "patron": patron,
            "message": message,
        },
    )


@router.post("/me/preferences")
def my_preferences_save(
    request: Request,
    receive_notifications: str = Form(default=""),
    csrf_token: str = Form(...),
    user: AppUser = Depends(require_web_user),
    patron: Patron = Depends(get_web_patron),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    patron.receive_notifications = receive_notifications == "on"
    SqlPatronRepository(session).update(patron)
    return RedirectResponse(
        "/ui/me/preferences?message=Preferences+saved.", status_code=303
    )
