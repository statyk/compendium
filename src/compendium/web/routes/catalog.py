from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.holds import HoldService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import get_web_user, require_web_permission, require_web_user
from compendium.web.jinja import templates

router = APIRouter()


def _holds_svc(session: Session) -> HoldService:
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_expiry_days=get_settings().hold_expiry_days,
    )


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    """Render a template, setting CSRF cookie on response if a fresh token was generated."""
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean, status_code=status_code)
    if fresh:
        set_csrf_cookie(resp, fresh, get_settings().jwt_secret_key)
    return resp


@router.get("/catalog")
def catalog_search(
    request: Request,
    q: str = "",
    field: str = "all",
    user=Depends(get_web_user),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    works = []
    if settings.guest_search_enabled or user is not None:
        if q:
            works = SqlWorkRepository(session).search(q, field=field)
        else:
            works = SqlWorkRepository(session).list(limit=50)
    return _render(
        "catalog/search.html",
        request,
        {"request": request, "user": user, "works": works, "q": q, "field": field},
    )


@router.get("/catalog/search-results", response_class=HTMLResponse)
def catalog_search_results(
    request: Request,
    q: str = "",
    field: str = "all",
    user=Depends(get_web_user),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    works = []
    if settings.guest_search_enabled or user is not None:
        if q:
            works = SqlWorkRepository(session).search(q, field=field)
        else:
            works = SqlWorkRepository(session).list(limit=50)
    return templates.TemplateResponse(
        request,
        "_partials/work_list.html",
        {"works": works, "q": q},
    )


@router.get("/catalog/{work_id:int}")
def work_detail(
    work_id: int,
    request: Request,
    user=Depends(get_web_user),
    session: Session = Depends(get_session),
):
    work = SqlWorkRepository(session).get(work_id)
    if work is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": "Work not found"},
            status_code=404,
        )
    patron = None
    if user is not None:
        patron = SqlPatronRepository(session).get_by_user_id(user.id)
    return _render(
        "catalog/detail.html",
        request,
        {"request": request, "user": user, "work": work, "patron": patron, "error": None},
    )


@router.post("/catalog/{work_id:int}/hold", response_class=HTMLResponse)
def place_hold(
    work_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user=Depends(require_web_user),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    patron = SqlPatronRepository(session).get_by_user_id(user.id)
    if patron is None:
        return HTMLResponse("<p class='error-banner'>No patron account linked to your user.</p>")
    try:
        _holds_svc(session).place(work_id, patron.library_card_number)
        return HTMLResponse("<p class='success-banner'>Hold placed successfully.</p>")
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<p class='error-banner'>{escape(str(exc))}</p>")


@router.post("/catalog/{work_id:int}/hold-for", response_class=HTMLResponse)
def place_hold_for(
    work_id: int,
    request: Request,
    card_number: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user=Depends(require_web_permission("hold.place.any")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    card = card_number.strip()
    if not card:
        return HTMLResponse("<p class='error-banner'>Enter a patron card number.</p>")
    try:
        _holds_svc(session).place(work_id, card)
        return HTMLResponse(
            f"<p class='success-banner'>Hold placed for card {escape(card)}.</p>"
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<p class='error-banner'>{escape(str(exc))}</p>")
