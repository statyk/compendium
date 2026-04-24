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
from compendium.domain.enums import CreatorRole, ItemStatus
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.services.auth import has_permission
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.discovery import DiscoveryService
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
    settings = get_settings()
    # Wire notification_svc so the immediate-promote path (and _release_held_item
    # reassignments) queues a hold_ready email when a hold becomes AVAILABLE.
    from compendium.repositories.sql.notification_repository import (
        SqlNotificationRepository,
    )
    from compendium.services.notifications import NotificationService

    notifs = NotificationService(
        notification_repo=SqlNotificationRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        settings=settings,
    )
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
        hold_expiry_days=settings.hold_expiry_days,
        hold_pickup_days=settings.hold_pickup_days,
        notification_svc=notifs,
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


def _discovery(session: Session) -> DiscoveryService:
    return DiscoveryService(work_repo=SqlWorkRepository(session))


def _parse_csv(s: str) -> list[str]:
    return [tok.strip() for tok in s.split(",") if tok.strip()]


def _parse_int(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _filters_qs(*, q: str, field: str, media: list[str], decade: int | None, avail: bool) -> str:
    from urllib.parse import urlencode

    params = [("q", q), ("field", field)]
    if media:
        params.append(("media", ",".join(media)))
    if decade is not None:
        params.append(("decade", str(decade)))
    if avail:
        params.append(("avail", "1"))
    return urlencode(params)


@router.get("/catalog")
def catalog_search(
    request: Request,
    q: str = "",
    field: str = "all",
    media: str = "",
    decade: str = "",
    avail: str = "",
    page: int = 1,
    user=Depends(get_web_user),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    if not (settings.guest_search_enabled or user is not None):
        return _render(
            "catalog/search.html",
            request,
            {
                "request": request,
                "user": user,
                "page": None,
                "q": q,
                "field": field,
                "media": [],
                "decade": None,
                "avail": False,
                "new_arrivals": [],
                "recently_returned": [],
                "show_landing": False,
            },
        )

    media_codes = _parse_csv(media)
    decade_int = _parse_int(decade)
    available_only = avail in ("1", "true", "on", "yes")
    has_filters = bool(q or media_codes or decade_int is not None or available_only)

    svc = _discovery(session)
    page_obj = svc.search(
        q,
        field=field,
        page=page,
        page_size=25,
        media_type_codes=media_codes,
        decade=decade_int,
        available_only=available_only,
    )
    new_arrivals: list = []
    recently_returned: list = []
    if not has_filters:
        new_arrivals = svc.new_arrivals()
        recently_returned = svc.recently_returned()
    qs = _filters_qs(q=q, field=field, media=media_codes, decade=decade_int, avail=available_only)
    return _render(
        "catalog/search.html",
        request,
        {
            "request": request,
            "user": user,
            "page": page_obj,
            "q": q,
            "field": field,
            "media": media_codes,
            "decade": decade_int,
            "avail": available_only,
            "filters_qs": qs,
            "new_arrivals": new_arrivals,
            "recently_returned": recently_returned,
            "show_landing": not has_filters,
        },
    )


@router.get("/catalog/search-results", response_class=HTMLResponse)
def catalog_search_results(
    request: Request,
    q: str = "",
    field: str = "all",
    media: str = "",
    decade: str = "",
    avail: str = "",
    page: int = 1,
    user=Depends(get_web_user),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    if not (settings.guest_search_enabled or user is not None):
        return templates.TemplateResponse(
            request, "_partials/work_list.html", {"page": None, "q": q}
        )
    media_codes = _parse_csv(media)
    decade_int = _parse_int(decade)
    available_only = avail in ("1", "true", "on", "yes")
    page_obj = _discovery(session).search(
        q,
        field=field,
        page=page,
        page_size=25,
        media_type_codes=media_codes,
        decade=decade_int,
        available_only=available_only,
    )
    qs = _filters_qs(q=q, field=field, media=media_codes, decade=decade_int, avail=available_only)
    return templates.TemplateResponse(
        request,
        "_partials/work_list.html",
        {"page": page_obj, "q": q, "filters_qs": qs},
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
    loans = SqlLoanRepository(session)
    item_due: dict[int, object] = {}
    for it in work.items:
        if it.status == ItemStatus.CHECKED_OUT.value:
            active = loans.get_active_for_item(it.id)
            if active is not None:
                item_due[it.id] = active.due_at
    has_loanable = SqlWorkRepository(session).has_loanable_item(work.id)
    # Librarian-only hold queue for this work.
    queue: list = []
    if user is not None and has_permission(user.role.permissions, "hold.view.any"):
        queue = SqlHoldRepository(session).queue_for_work(work.id)
    return _render(
        "catalog/detail.html",
        request,
        {
            "request": request,
            "user": user,
            "work": work,
            "patron": patron,
            "item_due": item_due,
            "has_loanable": has_loanable,
            "queue": queue,
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


@router.get("/catalog/{work_id:int}/creators")
def work_creators_page(
    work_id: int,
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
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
        "catalog/creators.html",
        request,
        {
            "request": request,
            "user": user,
            "work": work,
            "roles": [r.value for r in CreatorRole],
            "message": message,
            "error": error,
        },
    )


def _current_creators(work) -> list[tuple[str, str]]:
    return [(wc.creator.display_name, wc.role) for wc in work.creators]


def _creators_redirect(work_id: int, *, message: str | None = None, error: str | None = None) -> RedirectResponse:
    from urllib.parse import urlencode

    params: dict[str, str] = {}
    if message:
        params["message"] = message
    if error:
        params["error"] = error
    qs = ("?" + urlencode(params)) if params else ""
    return RedirectResponse(f"/ui/catalog/{work_id}/creators{qs}", status_code=303)


@router.post("/catalog/{work_id:int}/creators/add")
def work_creator_add(
    work_id: int,
    request: Request,
    name: str = Form(default=""),
    role: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission("work.edit")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    work = SqlWorkRepository(session).get(work_id)
    if work is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": "Work not found"},
            status_code=404,
        )
    new_list = _current_creators(work) + [(name, role)]
    try:
        _catalog_svc(session, user).replace_creators(work_id, new_list)
    except (BusinessRuleError, ValidationError) as exc:
        return _creators_redirect(work_id, error=str(exc))
    return _creators_redirect(work_id, message="Creator added.")


@router.post("/catalog/{work_id:int}/creators/remove")
def work_creator_remove(
    work_id: int,
    request: Request,
    creator_id: int = Form(...),
    role: str = Form(...),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission("work.edit")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    work = SqlWorkRepository(session).get(work_id)
    if work is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": "Work not found"},
            status_code=404,
        )
    new_list = [
        (wc.creator.display_name, wc.role)
        for wc in work.creators
        if not (wc.creator_id == creator_id and wc.role == role)
    ]
    try:
        _catalog_svc(session, user).replace_creators(work_id, new_list)
    except (BusinessRuleError, ValidationError) as exc:
        return _creators_redirect(work_id, error=str(exc))
    return _creators_redirect(work_id, message="Creator removed.")


@router.post("/catalog/{work_id:int}/creators/move")
def work_creator_move(
    work_id: int,
    request: Request,
    creator_id: int = Form(...),
    role: str = Form(...),
    direction: str = Form(...),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission("work.edit")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    work = SqlWorkRepository(session).get(work_id)
    if work is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": "Work not found"},
            status_code=404,
        )
    ordered = [(wc.creator_id, wc.role, wc.creator.display_name) for wc in work.creators]
    idx = next(
        (i for i, (cid, r, _) in enumerate(ordered) if cid == creator_id and r == role),
        None,
    )
    if idx is None:
        return _creators_redirect(work_id, error="Creator not found on this work.")
    if direction == "up" and idx > 0:
        ordered[idx - 1], ordered[idx] = ordered[idx], ordered[idx - 1]
    elif direction == "down" and idx < len(ordered) - 1:
        ordered[idx + 1], ordered[idx] = ordered[idx], ordered[idx + 1]
    new_list = [(name, r) for _cid, r, name in ordered]
    try:
        _catalog_svc(session, user).replace_creators(work_id, new_list)
    except (BusinessRuleError, ValidationError) as exc:
        return _creators_redirect(work_id, error=str(exc))
    return _creators_redirect(work_id, message="Order updated.")


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
