from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import (
    BusinessRuleError,
    ExternalLookupError,
    NotFoundError,
    ValidationError,
)
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.metadata import lookup_isbn, normalize_isbn, parse_open_library
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM_VIEW = "item.view"
_PERM_MANAGE = "item.delete"


def _catalog_svc(session: Session, actor: AppUser) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
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
        set_csrf_cookie(resp, fresh, get_settings().jwt_secret_key)
    return resp


def _partial(name: str, request: Request, ctx: dict):
    token, fresh = ensure_csrf(request)
    resp = templates.TemplateResponse(request, name, {**ctx, "csrf_token": token})
    if fresh:
        set_csrf_cookie(resp, fresh, get_settings().jwt_secret_key)
    return resp


# /items/new must be defined before /items/{barcode} so the literal segment wins.

@router.get("/items/new")
def item_new_form(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM_MANAGE)),
):
    return _render("items/new.html", request, {"request": request, "user": user, "error": None})


@router.post("/items/lookup", response_class=HTMLResponse)
def item_lookup(
    request: Request,
    isbn: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM_MANAGE)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    isbn_raw = isbn.strip()
    if not isbn_raw:
        return HTMLResponse("<p class='error-banner'>Please enter an ISBN.</p>")
    try:
        normalized = normalize_isbn(isbn_raw)
    except (ValidationError, Exception) as exc:
        return HTMLResponse(f"<p class='error-banner'>{exc}</p>")

    existing_work = SqlWorkRepository(session).get_by_isbn(normalized)
    if existing_work is not None:
        return _partial(
            "_partials/item_preview.html",
            request,
            {"isbn": normalized, "work": existing_work, "meta": None, "existing": True},
        )

    try:
        data = lookup_isbn(normalized)
    except ExternalLookupError as exc:
        return HTMLResponse(f"<p class='error-banner'>{exc}</p>")

    if not data:
        return HTMLResponse(
            f"<p class='error-banner'>ISBN {normalized} not found in Open Library. "
            "Check the ISBN and try again.</p>"
        )

    meta = parse_open_library(data, normalized)
    return _partial(
        "_partials/item_preview.html",
        request,
        {"isbn": normalized, "work": None, "meta": meta, "existing": False},
    )


@router.post("/items/new")
def item_create(
    request: Request,
    isbn: str = Form(),
    location: str = Form(default=""),
    call_number: str = Form(default=""),
    condition: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM_MANAGE)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        work, item = _catalog_svc(session, user).add_from_isbn(
            isbn.strip(),
            location=location.strip() or None,
        )
    except (BusinessRuleError, NotFoundError, ExternalLookupError, ValidationError) as exc:
        return _render(
            "items/new.html",
            request,
            {"request": request, "user": user, "error": str(exc)},
        )
    if call_number.strip():
        item.call_number = call_number.strip()
    if condition.strip():
        item.condition = condition.strip()
    return RedirectResponse(f"/ui/items/{item.barcode}", status_code=303)


@router.get("/items/{barcode}")
def item_detail(
    barcode: str,
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM_VIEW)),
    session: Session = Depends(get_session),
):
    item = SqlItemRepository(session).get_by_barcode(barcode)
    if item is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"Item '{barcode}' not found"},
            status_code=404,
        )
    return _render(
        "items/detail.html",
        request,
        {"request": request, "user": user, "item": item},
    )


@router.post("/items/{barcode}/withdraw", response_class=HTMLResponse)
def withdraw_item(
    barcode: str,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM_MANAGE)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _catalog_svc(session, user).withdraw_item(barcode)
        return HTMLResponse("<span class='error-banner'>Item withdrawn.</span>")
    except (BusinessRuleError, NotFoundError) as exc:
        return HTMLResponse(f"<span class='error-banner'>{exc}</span>")
