from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import (
    BusinessRuleError,
    NotFoundError,
    ValidationError,
)
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.holds import HoldService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import get_web_user, require_web_permission, require_web_user
from compendium.web.jinja import templates

router = APIRouter()


def _catalog_svc(session: Session, actor: AppUser) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="web",
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
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
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
        {
            "request": request,
            "user": user,
            "work": work,
            "patron": patron,
            "message": message,
            "error": error,
        },
    )


@router.get("/catalog/{work_id:int}/edit")
def work_edit_form(
    work_id: int,
    request: Request,
    user: AppUser = Depends(require_web_permission("work.edit")),
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
    return _render(
        "catalog/edit.html",
        request,
        {"request": request, "user": user, "work": work, "error": None},
    )


@router.post("/catalog/{work_id:int}/edit")
def work_edit_submit(
    work_id: int,
    request: Request,
    title: str = Form(default=""),
    subtitle: str = Form(default=""),
    publisher: str = Form(default=""),
    publication_year: str = Form(default=""),
    edition: str = Form(default=""),
    language: str = Form(default=""),
    description: str = Form(default=""),
    classification_scheme: str = Form(default=""),
    classification_code: str = Form(default=""),
    cover_image_url: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission("work.edit")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)

    year_val: int | None = None
    if publication_year.strip():
        try:
            year_val = int(publication_year.strip())
        except ValueError:
            work = SqlWorkRepository(session).get(work_id)
            return _render(
                "catalog/edit.html",
                request,
                {"request": request, "user": user, "work": work,
                 "error": "Publication year must be a number."},
            )

    try:
        _catalog_svc(session, user).update_work(
            work_id,
            title=title,
            subtitle=subtitle,
            publisher=publisher,
            publication_year=year_val,
            edition=edition,
            language=language,
            description=description,
            classification_scheme=classification_scheme,
            classification_code=classification_code,
            cover_image_url=cover_image_url,
        )
    except NotFoundError:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": "Work not found"},
            status_code=404,
        )
    except (BusinessRuleError, ValidationError) as exc:
        work = SqlWorkRepository(session).get(work_id)
        return _render(
            "catalog/edit.html",
            request,
            {"request": request, "user": user, "work": work, "error": str(exc)},
        )
    return RedirectResponse(
        f"/ui/catalog/{work_id}?message=Work+updated.", status_code=303
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
