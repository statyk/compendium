from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.repositories.sql.curated_list_repository import SqlCuratedListRepository
from compendium.services.curated_lists import CuratedListService
from compendium.domain.enums import CreatorRole, ItemStatus
from compendium.domain.errors import (
    BusinessRuleError,
    NotFoundError,
    ValidationError,
)
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.counters import SqlCounterRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.auth import has_permission
from compendium.services.catalog import CatalogService
from compendium.services.discovery import DiscoveryService
from compendium.services.first_run import first_run_status
from compendium.services.holds import HoldService
from compendium.services.site_settings import get_site_setting, set_site_setting
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.services.calendar import CalendarService
from compendium.web.deps import get_calendar_svc, get_web_user, require_web_permission, require_web_user
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
        counter_repo=SqlCounterRepository(session),
    )


def _holds_svc(session: Session, calendar_svc: CalendarService | None = None) -> HoldService:
    settings = get_settings()
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
        hold_expiry_days=get_site_setting("hold_expiry_days"),
        hold_pickup_days=get_site_setting("hold_pickup_days"),
        calendar_svc=calendar_svc,
        notification_svc=notifs,
    )


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    """Render a template, setting CSRF cookie on response if a fresh token was generated."""
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean, status_code=status_code)
    if fresh:
        set_csrf_cookie(resp, fresh)
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


_VALID_ORDER_BY = {"title", "author", "recent", "relevance"}


def _resolve_order_by(order_by: str, q: str, field: str) -> str:
    """Resolve the effective sort. Empty/invalid input means 'no explicit
    choice' → relevance for All-Fields keyword searches, title otherwise.
    An explicit 'relevance' off the FTS path degrades to title so the
    dropdown state stays truthful (ILIKE paths have no rank)."""
    is_fts = bool(q.strip()) and field == "all"
    if order_by not in _VALID_ORDER_BY:
        order_by = ""
    if not order_by:
        return "relevance" if is_fts else "title"
    if order_by == "relevance" and not is_fts:
        return "title"
    return order_by


def _filters_qs(
    *, q: str, field: str, media: list[str], decade: int | None, avail: bool,
    include_withdrawn: bool = False,
    order_by: str = "title",
) -> str:
    from urllib.parse import urlencode

    params = [("q", q), ("field", field)]
    if media:
        params.append(("media", ",".join(media)))
    if decade is not None:
        params.append(("decade", str(decade)))
    if avail:
        params.append(("avail", "1"))
    if include_withdrawn:
        params.append(("include_withdrawn", "1"))
    if order_by != "title":
        params.append(("order_by", order_by))
    return urlencode(params)


@router.get("/catalog")
def catalog_search(
    request: Request,
    q: str = "",
    field: str = "all",
    media: str = "",
    decade: str = "",
    avail: str = "",
    include_withdrawn: str = "",
    order_by: str = "",
    page: int = 1,
    user=Depends(get_web_user),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    order_by = _resolve_order_by(order_by, q, field)
    if not (get_site_setting("guest_search_enabled") or user is not None):
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
                "include_withdrawn": False,
                "can_include_withdrawn": False,
                "order_by": order_by,
                "new_arrivals": [],
                "recently_returned": [],
                "featured_lists": [],
                "show_landing": False,
                "first_run": None,
            },
        )

    media_codes = _parse_csv(media)
    decade_int = _parse_int(decade)
    available_only = avail in ("1", "true", "on", "yes")
    can_include_withdrawn = user is not None and has_permission(user.role.permissions, "item.edit")
    include_withdrawn_flag = can_include_withdrawn and include_withdrawn in ("1", "true", "on", "yes")
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
        include_withdrawn_only=include_withdrawn_flag,
        order_by=order_by,
    )
    new_arrivals: list = []
    recently_returned: list = []
    featured_lists: list = []
    if not has_filters:
        new_arrivals = svc.new_arrivals(include_withdrawn_only=include_withdrawn_flag)
        recently_returned = svc.recently_returned(include_withdrawn_only=include_withdrawn_flag)
        featured_lists = CuratedListService(
            curated_list_repo=SqlCuratedListRepository(session),
            work_repo=SqlWorkRepository(session),
        ).list(featured_only=True, public_only=True, limit=10, offset=0)
    qs = _filters_qs(
        q=q, field=field, media=media_codes, decade=decade_int, avail=available_only,
        include_withdrawn=include_withdrawn_flag, order_by=order_by,
    )
    first_run = None
    if (
        user is not None
        and not has_filters
        and has_permission(user.role.permissions, "system.manage")
        and not get_site_setting("first_run_dismissed")
    ):
        status = first_run_status(session)
        if not status.all_done:
            first_run = status
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
            "include_withdrawn": include_withdrawn_flag,
            "can_include_withdrawn": can_include_withdrawn,
            "order_by": order_by,
            "filters_qs": qs,
            "new_arrivals": new_arrivals,
            "recently_returned": recently_returned,
            "featured_lists": featured_lists,
            "show_landing": not has_filters,
            "first_run": first_run,
        },
    )


@router.post("/first-run/dismiss")
def first_run_dismiss(
    request: Request,
    csrf_token: str = Form(...),
    user: AppUser = Depends(require_web_permission("system.manage")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    set_site_setting(
        "first_run_dismissed", True, session=session,
        updated_by_id=user.id, source="web",
    )
    return HTMLResponse("")


@router.get("/catalog/search-results", response_class=HTMLResponse)
def catalog_search_results(
    request: Request,
    q: str = "",
    field: str = "all",
    media: str = "",
    decade: str = "",
    avail: str = "",
    include_withdrawn: str = "",
    order_by: str = "",
    page: int = 1,
    user=Depends(get_web_user),
    session: Session = Depends(get_session),
):
    order_by = _resolve_order_by(order_by, q, field)
    if not (get_site_setting("guest_search_enabled") or user is not None):
        return templates.TemplateResponse(
            request, "_partials/work_list.html", {"page": None, "q": q, "field": field, "user": user}
        )
    media_codes = _parse_csv(media)
    decade_int = _parse_int(decade)
    available_only = avail in ("1", "true", "on", "yes")
    can_include_withdrawn = user is not None and has_permission(user.role.permissions, "item.edit")
    include_withdrawn_flag = can_include_withdrawn and include_withdrawn in ("1", "true", "on", "yes")
    page_obj = _discovery(session).search(
        q,
        field=field,
        page=page,
        page_size=25,
        media_type_codes=media_codes,
        decade=decade_int,
        available_only=available_only,
        include_withdrawn_only=include_withdrawn_flag,
        order_by=order_by,
    )
    qs = _filters_qs(
        q=q, field=field, media=media_codes, decade=decade_int, avail=available_only,
        include_withdrawn=include_withdrawn_flag, order_by=order_by,
    )
    return templates.TemplateResponse(
        request,
        "_partials/work_list.html",
        {"page": page_obj, "q": q, "field": field, "filters_qs": qs, "user": user},
    )


@router.get("/catalog/suggest", response_class=HTMLResponse)
def catalog_search_suggest(
    request: Request,
    q: str = "",
    user=Depends(get_web_user),
    session: Session = Depends(get_session),
):
    if not (get_site_setting("guest_search_enabled") or user is not None):
        return HTMLResponse("")
    works = _discovery(session).suggest(q, limit=8)
    return templates.TemplateResponse(
        request, "_partials/suggest.html", {"works": works, "q": q}
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
    non_withdrawn = [
        it for it in work.items if it.status != ItemStatus.WITHDRAWN.value
    ]
    copies_available = sum(
        1 for it in non_withdrawn if it.status == ItemStatus.AVAILABLE.value
    )
    earliest_due = (
        min(item_due.values()) if (copies_available == 0 and item_due) else None
    )
    has_loanable = SqlWorkRepository(session).has_loanable_item(work.id)
    all_withdrawn = bool(work.items) and all(
        it.status == ItemStatus.WITHDRAWN.value for it in work.items
    )
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
            "copies_available": copies_available,
            "copies_total": len(non_withdrawn),
            "earliest_due": earliest_due,
            "has_loanable": has_loanable,
            "all_withdrawn": all_withdrawn,
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
    from compendium.services.metadata import get_book_primary_adapter_name
    return _render(
        "catalog/edit.html",
        request,
        {
            "request": request,
            "user": user,
            "work": work,
            "error": None,
            "book_primary_source": get_book_primary_adapter_name(),
        },
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

    from compendium.services.metadata import get_book_primary_adapter_name as _gbpan

    year_val: int | None = None
    if publication_year.strip():
        try:
            year_val = int(publication_year.strip())
        except ValueError:
            work = SqlWorkRepository(session).get(work_id)
            return _render(
                "catalog/edit.html",
                request,
                {
                    "request": request,
                    "user": user,
                    "work": work,
                    "error": "Publication year must be a number.",
                    "book_primary_source": _gbpan(),
                },
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
            {
                "request": request,
                "user": user,
                "work": work,
                "error": str(exc),
                "book_primary_source": _gbpan(),
            },
        )
    return RedirectResponse(
        f"/ui/catalog/{work_id}?message=Work+updated.", status_code=303
    )


@router.get("/catalog/{work_id:int}/refresh-metadata")
def work_refresh_preview(
    work_id: int,
    request: Request,
    source: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("work.edit")),
    session: Session = Depends(get_session),
):
    """Preview a metadata refresh — fetches upstream + computes diff. No DB writes."""
    try:
        report = _catalog_svc(session, user).refresh_metadata(
            work_id, dry_run=True, bypass_cache=True,
            source=source if source else None,
        )
    except NotFoundError:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": "Work not found"},
            status_code=404,
        )
    work = SqlWorkRepository(session).get(work_id)
    return _render(
        "catalog/refresh_preview.html",
        request,
        {
            "request": request,
            "user": user,
            "work": work,
            "report": report,
        },
    )


@router.post("/catalog/{work_id:int}/refresh-metadata")
def work_refresh_apply(
    work_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    source: str | None = Form(default=None),
    user: AppUser = Depends(require_web_permission("work.edit")),
    session: Session = Depends(get_session),
):
    """Apply a metadata refresh. Re-fetches upstream (idempotent) and commits."""
    check_csrf_form(request, csrf_token)
    try:
        report = _catalog_svc(session, user).refresh_metadata(
            work_id, dry_run=False, bypass_cache=True,
            source=source if source else None,
        )
    except NotFoundError:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": "Work not found"},
            status_code=404,
        )
    if not report.found:
        return RedirectResponse(
            f"/ui/catalog/{work_id}/edit?error={quote(report.error or 'refresh_failed')}",
            status_code=303,
        )
    if not report.planned:
        return RedirectResponse(
            f"/ui/catalog/{work_id}/edit?message=No+changes+to+apply.",
            status_code=303,
        )
    summary = ", ".join(sorted(report.planned.keys()))
    return RedirectResponse(
        f"/ui/catalog/{work_id}/edit?message=Refreshed+from+{report.source}:+{quote(summary)}",
        status_code=303,
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
    calendar_svc: CalendarService = Depends(get_calendar_svc),
):
    check_csrf_form(request, csrf_token)
    patron = SqlPatronRepository(session).get_by_user_id(user.id)
    if patron is None:
        return HTMLResponse("<p class='error-banner'>No patron account linked to your user.</p>")
    try:
        _holds_svc(session, calendar_svc).place(work_id, patron.library_card_number)
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
    calendar_svc: CalendarService = Depends(get_calendar_svc),
):
    check_csrf_form(request, csrf_token)
    card = card_number.strip()
    if not card:
        return HTMLResponse("<p class='error-banner'>Enter a patron card number.</p>")
    try:
        _holds_svc(session, calendar_svc).place(work_id, card)
        return HTMLResponse(
            f"<p class='success-banner'>Hold placed for card {escape(card)}.</p>"
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<p class='error-banner'>{escape(str(exc))}</p>")
