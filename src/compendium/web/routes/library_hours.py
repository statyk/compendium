"""Web UI for library hours and closed-date calendar admin."""
from __future__ import annotations

from datetime import date, time
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.calendar_repository import (
    SqlClosedDateRepository,
    SqlLibraryHoursRepository,
)
from compendium.services.audit import AuditService
from compendium.services.calendar import CalendarService
from compendium.services.site_settings import get_site_setting
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "calendar.manage"
_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _svc(session: Session, actor: AppUser) -> CalendarService:
    return CalendarService(
        hours_repo=SqlLibraryHoursRepository(session),
        closed_date_repo=SqlClosedDateRepository(session),
        timezone=get_site_setting("library_timezone"),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=actor.username,
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


# ------------------------------------------------------------------
# Library Hours
# ------------------------------------------------------------------

@router.get("/admin/library-hours")
def library_hours_list(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    hours = SqlLibraryHoursRepository(session).list()
    return _render(
        "admin/library_hours.html",
        request,
        {
            "request": request,
            "user": user,
            "hours": hours,
            "weekday_names": _WEEKDAY_NAMES,
            "library_timezone": get_site_setting("library_timezone"),
            "message": message,
            "error": error,
        },
    )


@router.post("/admin/library-hours/update")
async def library_hours_update_all(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, str(form.get("csrf_token", "")))
    try:
        # Parse and validate ALL rows before writing any (atomic UX + one
        # transaction via get_session).
        parsed: list[tuple[int, bool, time | None, time | None]] = []
        for weekday in range(7):
            parsed.append(
                (
                    weekday,
                    form.get(f"is_open_{weekday}", "") == "on",
                    _parse_time(str(form.get(f"open_time_{weekday}", ""))),
                    _parse_time(str(form.get(f"close_time_{weekday}", ""))),
                )
            )
        svc = _svc(session, user)
        for weekday, is_open, open_t, close_t in parsed:
            svc.update_weekday(weekday, is_open=is_open, open_time=open_t, close_time=close_t)
        return RedirectResponse("/ui/admin/library-hours?message=Hours+updated.", status_code=303)
    except (ValidationError, NotFoundError, BusinessRuleError, ValueError) as exc:
        return RedirectResponse(
            f"/ui/admin/library-hours?error={quote(str(exc))}", status_code=303
        )


# ------------------------------------------------------------------
# Closed Dates
# ------------------------------------------------------------------

@router.get("/admin/closed-dates")
def closed_dates_list(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    dates = SqlClosedDateRepository(session).list(limit=200)
    return _render(
        "admin/closed_dates.html",
        request,
        {
            "request": request,
            "user": user,
            "closed_dates": dates,
            "message": message,
            "error": error,
        },
    )


@router.post("/admin/closed-dates/new")
def closed_date_create(
    request: Request,
    start_date: str = Form(),
    end_date: str = Form(default=""),
    label: str = Form(default=""),
    recurs_annually: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date) if end_date else None
        _svc(session, user).add_closed_date(
            start,
            end,
            label=label or None,
            recurs_annually=(recurs_annually == "on"),
        )
        return RedirectResponse("/ui/admin/closed-dates?message=Closed+date+added.", status_code=303)
    except (ValidationError, BusinessRuleError, ValueError) as exc:
        return RedirectResponse(
            f"/ui/admin/closed-dates?error={quote(str(exc))}", status_code=303
        )


@router.post("/admin/closed-dates/{closed_date_id}/delete")
def closed_date_delete(
    closed_date_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _svc(session, user).delete_closed_date(closed_date_id)
        return RedirectResponse(
            "/ui/admin/closed-dates?message=Closed+date+deleted.", status_code=303
        )
    except NotFoundError as exc:
        return RedirectResponse(
            f"/ui/admin/closed-dates?error={quote(str(exc))}", status_code=303
        )


def _parse_time(s: str) -> time | None:
    s = s.strip()
    if not s:
        return None
    try:
        parts = s.split(":")
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError) as exc:
        raise ValueError(f"Invalid time '{s}' — expected HH:MM format.") from exc
