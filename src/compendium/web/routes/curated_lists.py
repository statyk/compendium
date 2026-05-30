from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.curated_list_repository import SqlCuratedListRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.curated_lists import CuratedListService, _MISSING
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import get_web_user, require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "curatedlist.manage"


def _svc(session: Session, actor: AppUser) -> CuratedListService:
    return CuratedListService(
        curated_list_repo=SqlCuratedListRepository(session),
        work_repo=SqlWorkRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
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


@router.get("/curated-lists")
def list_curated_lists(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    lists = SqlCuratedListRepository(session).list(limit=500)
    return _render(
        "curated_lists/list.html",
        request,
        {"request": request, "user": user, "lists": lists},
    )


@router.get("/curated-lists/new")
def new_curated_list_form(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    return _render(
        "curated_lists/new.html",
        request,
        {"request": request, "user": user, "error": None},
    )


@router.post("/curated-lists/new")
def create_curated_list(
    request: Request,
    name: str = Form(),
    description: str = Form(default=""),
    is_public: str = Form(default=""),
    is_featured: str = Form(default=""),
    display_order: str = Form(default="0"),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        order = int(display_order) if display_order.strip() else 0
    except ValueError:
        order = 0
    try:
        cl = _svc(session, user).create(
            name=name.strip(),
            description=description.strip() or None,
            is_public=bool(is_public),
            is_featured=bool(is_featured),
            display_order=order,
        )
    except (BusinessRuleError, ValidationError) as exc:
        return _render(
            "curated_lists/new.html",
            request,
            {
                "request": request,
                "user": user,
                "error": str(exc),
                "name": name,
                "description": description,
                "is_public": bool(is_public),
                "is_featured": bool(is_featured),
                "display_order": order,
            },
        )
    return RedirectResponse(f"/ui/curated-lists/{cl.slug}", status_code=303)


@router.get("/curated-lists/{slug}")
def curated_list_detail(
    slug: str,
    request: Request,
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    cl = SqlCuratedListRepository(session).get_by_slug(slug)
    if cl is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"Curated list '{slug}' not found"},
            status_code=404,
        )
    return _render(
        "curated_lists/detail.html",
        request,
        {
            "request": request,
            "user": user,
            "cl": cl,
            "entries": cl.entries,
            "error": error,
        },
    )


@router.post("/curated-lists/{slug}/edit")
def edit_curated_list(
    slug: str,
    request: Request,
    name: str = Form(),
    description: str = Form(default=""),
    is_public: str = Form(default=""),
    is_featured: str = Form(default=""),
    display_order: str = Form(default="0"),
    new_slug: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    cl = SqlCuratedListRepository(session).get_by_slug(slug)
    if cl is None:
        return RedirectResponse(f"/ui/curated-lists?error={quote('List not found')}", status_code=303)
    try:
        order = int(display_order) if display_order.strip() else 0
    except ValueError:
        order = 0
    try:
        updated = _svc(session, user).update(
            cl.id,
            name=name.strip() if name.strip() else _MISSING,
            description=description.strip() or None,
            is_public=bool(is_public),
            is_featured=bool(is_featured),
            display_order=order,
            slug=new_slug.strip() if new_slug.strip() else _MISSING,
        )
    except (BusinessRuleError, NotFoundError, ValidationError) as exc:
        return RedirectResponse(
            f"/ui/curated-lists/{slug}?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(f"/ui/curated-lists/{updated.slug}", status_code=303)


@router.post("/curated-lists/{slug}/delete")
def delete_curated_list(
    slug: str,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    cl = SqlCuratedListRepository(session).get_by_slug(slug)
    if cl is None:
        return RedirectResponse("/ui/curated-lists", status_code=303)
    name = cl.name
    try:
        _svc(session, user).delete(cl.id)
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/curated-lists/{slug}?error={quote(str(exc))}", status_code=303
        )
    msg = f"Deleted ‘{name}’"
    return RedirectResponse(f"/ui/curated-lists?msg={quote(msg)}", status_code=303)


@router.post("/curated-lists/{slug}/works/add")
def add_work_to_list(
    slug: str,
    request: Request,
    work_id: int = Form(),
    annotation: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    cl = SqlCuratedListRepository(session).get_by_slug(slug)
    if cl is None:
        return RedirectResponse(f"/ui/curated-lists?error={quote('List not found')}", status_code=303)
    try:
        _svc(session, user).add_work(
            cl.id,
            work_id=work_id,
            annotation=annotation.strip() or None,
        )
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/curated-lists/{slug}?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(f"/ui/curated-lists/{slug}", status_code=303)


@router.post("/curated-lists/{slug}/works/{work_id}/remove")
def remove_work_from_list(
    slug: str,
    work_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    cl = SqlCuratedListRepository(session).get_by_slug(slug)
    if cl is None:
        return RedirectResponse(f"/ui/curated-lists?error={quote('List not found')}", status_code=303)
    try:
        _svc(session, user).remove_work(cl.id, work_id)
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/curated-lists/{slug}?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(f"/ui/curated-lists/{slug}", status_code=303)


@router.get("/lists")
def public_list_index(
    request: Request,
    user=Depends(get_web_user),
    session: Session = Depends(get_session),
):
    from compendium.services.site_settings import get_site_setting

    if not (get_site_setting("guest_search_enabled") or user is not None):
        return RedirectResponse("/ui/catalog", status_code=302)
    svc = CuratedListService(
        curated_list_repo=SqlCuratedListRepository(session),
        work_repo=SqlWorkRepository(session),
    )
    lists = svc.list(public_only=True, limit=100, offset=0)
    return _render("lists/index.html", request, {"lists": lists, "user": user})


@router.get("/lists/{slug}")
def public_list_view(
    slug: str,
    request: Request,
    user=Depends(get_web_user),
    session: Session = Depends(get_session),
):
    from compendium.services.site_settings import get_site_setting

    if not (get_site_setting("guest_search_enabled") or user is not None):
        return RedirectResponse("/ui/catalog", status_code=302)
    svc = CuratedListService(
        curated_list_repo=SqlCuratedListRepository(session),
        work_repo=SqlWorkRepository(session),
    )
    try:
        cl = svc.get_by_slug(slug)
    except NotFoundError:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="List not found")
    if not cl.is_public:
        # Non-public: only show to users with curatedlist.manage
        from compendium.services.auth import has_permission

        if user is None or not has_permission(user.role.permissions, _PERM):
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="List not found")
    return _render(
        "lists/view.html",
        request,
        {"cl": cl, "entries": cl.entries, "user": user},
    )


@router.post("/curated-lists/{slug}/works/reorder")
def reorder_works(
    slug: str,
    request: Request,
    work_order: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    cl = SqlCuratedListRepository(session).get_by_slug(slug)
    if cl is None:
        return RedirectResponse(f"/ui/curated-lists?error={quote('List not found')}", status_code=303)
    try:
        raw = [s.strip() for s in work_order.split(",") if s.strip()]
        ordered_ids = [int(x) for x in raw]
        _svc(session, user).reorder(cl.id, ordered_ids)
    except (BusinessRuleError, NotFoundError, ValueError) as exc:
        return RedirectResponse(
            f"/ui/curated-lists/{slug}?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(f"/ui/curated-lists/{slug}", status_code=303)
