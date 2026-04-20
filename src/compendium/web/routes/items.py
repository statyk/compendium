from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
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
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.metadata import (
    lookup_metadata,
    musicbrainz_search_title,
    normalize_isbn,
    normalize_upc,
    open_library_search_title,
    pick_classification_code,
    tmdb_search_title,
)
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM_VIEW = "item.view"
_PERM_MANAGE = "item.delete"

_MBID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_FILM_TYPES = {"dvd", "bluray", "vhs"}
_MUSIC_TYPES = {"vinyl", "cd"}


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


def _detect_kind(raw: str, media_type: str) -> tuple[str, str]:
    """Return (identifier_kind, normalised_value) based on media type and input format."""
    stripped = raw.strip()
    if media_type == "book":
        digits = re.sub(r"[\s\-]", "", stripped)
        if digits.isdigit() and len(digits) in (10, 13):
            return "isbn", normalize_isbn(stripped)
        return "title", stripped
    if media_type in _FILM_TYPES:
        if stripped.isdigit():
            return "tmdb_id", stripped
        return "title", stripped
    if _MBID_RE.match(stripped):
        return "mbid", stripped
    if media_type in _MUSIC_TYPES:
        digits = re.sub(r"[\s\-]", "", stripped)
        if digits.isdigit() and len(digits) in (8, 12, 13):
            return "upc", normalize_upc(raw)
        return "title", stripped
    return "upc", normalize_upc(raw)


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
    media_type: str = Form(default="book"),
    identifier: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM_MANAGE)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    raw = identifier.strip()
    if not raw:
        return HTMLResponse("<p class='error-banner'>Please enter an identifier.</p>")

    mt = media_type.strip()
    try:
        kind, value = _detect_kind(raw, mt)
    except (ValidationError, Exception) as exc:
        return HTMLResponse(f"<p class='error-banner'>{escape(str(exc))}</p>")

    # Title search → show candidate picker, not a preview.
    if kind == "title":
        try:
            if mt == "book":
                candidates = open_library_search_title(value)
            elif mt in _MUSIC_TYPES:
                candidates = musicbrainz_search_title(value, media_type=mt)
            else:
                candidates = tmdb_search_title(value)
        except ExternalLookupError as exc:
            return HTMLResponse(f"<p class='error-banner'>{escape(str(exc))}</p>")
        if not candidates:
            return HTMLResponse(
                f"<p class='error-banner'>No results for '{escape(value)}'. "
                "Try a different title.</p>"
            )
        return _partial(
            "_partials/title_candidates.html",
            request,
            {"media_type": mt, "query": value, "candidates": candidates},
        )

    work_repo = SqlWorkRepository(session)
    existing_work = None
    if kind == "isbn":
        existing_work = work_repo.get_by_isbn(value)
    elif kind == "upc":
        existing_work = work_repo.get_by_upc(value)

    branch = SqlBranchRepository(session).get_default()
    scheme = branch.default_classification_scheme if branch else "none"

    if existing_work is not None:
        return _partial(
            "_partials/item_preview.html",
            request,
            {
                "media_type": mt,
                "identifier_kind": kind,
                "identifier_value": value,
                "work": existing_work,
                "meta": None,
                "existing": True,
                "suggested_call_number": existing_work.classification_code,
            },
        )

    try:
        meta = lookup_metadata(mt, kind, value)
    except ExternalLookupError as exc:
        return HTMLResponse(f"<p class='error-banner'>{escape(str(exc))}</p>")

    if not meta:
        return HTMLResponse(
            f"<p class='error-banner'>No metadata found for {escape(kind)} "
            f"'{escape(value)}'. Check the identifier and try again.</p>"
        )

    suggested = pick_classification_code(scheme, meta) if scheme != "none" else None

    return _partial(
        "_partials/item_preview.html",
        request,
        {
            "media_type": mt,
            "identifier_kind": kind,
            "identifier_value": value,
            "work": None,
            "meta": meta,
            "existing": False,
            "suggested_call_number": suggested,
        },
    )


@router.post("/items/new")
def item_create(
    request: Request,
    media_type: str = Form(default="book"),
    identifier_kind: str = Form(default="isbn"),
    identifier_value: str = Form(default=""),
    location: str = Form(default=""),
    call_number: str = Form(default=""),
    condition: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM_MANAGE)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        work, item = _catalog_svc(session, user).add_from_lookup(
            media_type.strip(),
            identifier_kind.strip(),
            identifier_value.strip(),
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
        return HTMLResponse(f"<span class='error-banner'>{escape(str(exc))}</span>")
