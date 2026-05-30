from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.enums import LoanRestrictionReason
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
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.counters import SqlCounterRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.item_notes import ItemNoteService
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
_PERM_EDIT = "item.edit"

_REASON_CHOICES = [
    (LoanRestrictionReason.REFERENCE.value, "Reference"),
    (LoanRestrictionReason.IN_LIBRARY_USE.value, "In-library use only"),
    (LoanRestrictionReason.ARCHIVE.value, "Archive / preservation"),
    (LoanRestrictionReason.STAFF_ONLY.value, "Staff only"),
    (LoanRestrictionReason.DISPLAY.value, "On display"),
    (LoanRestrictionReason.OTHER.value, "Other (explain in note)"),
]

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
        hold_repo=SqlHoldRepository(session),
        counter_repo=SqlCounterRepository(session),
        item_note_repo=SqlItemNoteRepository(session),
    )


def _note_svc(session: Session, actor: AppUser) -> ItemNoteService:
    return ItemNoteService(
        item_note_repo=SqlItemNoteRepository(session),
        item_repo=SqlItemRepository(session),
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


def _partial(name: str, request: Request, ctx: dict):
    token, fresh = ensure_csrf(request)
    resp = templates.TemplateResponse(request, name, {**ctx, "csrf_token": token})
    if fresh:
        set_csrf_cookie(resp, fresh)
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
    work_id: int | None = Query(default=None),
    media_type: str | None = Query(default=None),
    added: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission(_PERM_MANAGE)),
    session: Session = Depends(get_session),
):
    ctx: dict = {
        "request": request,
        "user": user,
        "error": None,
        "preset_media_type": media_type or "book",
        "added_barcode": added,
        "added_title": None,
        "added_work_id": work_id,
        "copy_work": None,
    }
    if work_id:
        work = SqlWorkRepository(session).get(work_id)
        if added:
            ctx["added_title"] = work.title if work else None
        else:
            ctx["copy_work"] = work
    return _render("items/new.html", request, ctx)


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


@router.get("/items/new/manual")
def item_new_manual_form(
    request: Request,
    media_type: str | None = Query(default=None),
    added: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission(_PERM_MANAGE)),
):
    return _render(
        "items/new_manual.html",
        request,
        {
            "request": request,
            "user": user,
            "error": None,
            "form": {"media_type": media_type or "book"},
            "added_barcode": added,
        },
    )


@router.post("/items/new/manual")
def item_create_manual(
    request: Request,
    media_type: str = Form(default="book"),
    title: str = Form(default=""),
    authors: str = Form(default=""),
    publisher: str = Form(default=""),
    year: str = Form(default=""),
    isbn: str = Form(default=""),
    upc: str = Form(default=""),
    description: str = Form(default=""),
    location: str = Form(default=""),
    call_number: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM_MANAGE)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)

    form = {
        "media_type": media_type,
        "title": title,
        "authors": authors,
        "publisher": publisher,
        "year": year,
        "isbn": isbn,
        "upc": upc,
        "description": description,
        "location": location,
        "call_number": call_number,
    }

    parsed_year: int | None = None
    if year.strip():
        try:
            parsed_year = int(year.strip())
        except ValueError:
            return _render(
                "items/new_manual.html",
                request,
                {"request": request, "user": user, "error": "Year must be a number.", "form": form},
            )

    author_list = [a.strip() for a in authors.split(",") if a.strip()]

    try:
        _work, item = _catalog_svc(session, user).add_manual(
            media_type.strip(),
            title,
            authors=author_list,
            publisher=publisher or None,
            publication_year=parsed_year,
            isbn=isbn.strip() or None,
            upc=upc.strip() or None,
            description=description or None,
            location=location.strip() or None,
        )
    except (BusinessRuleError, NotFoundError, ValidationError) as exc:
        return _render(
            "items/new_manual.html",
            request,
            {"request": request, "user": user, "error": str(exc), "form": form},
        )
    if call_number.strip():
        item.call_number = call_number.strip()
    mt = media_type.strip()
    return RedirectResponse(
        f"/ui/items/new/manual?media_type={quote(mt)}&added={quote(item.barcode)}",
        status_code=303,
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
    copy_work_id: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM_MANAGE)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    svc = _catalog_svc(session, user)
    if copy_work_id.strip():
        # Fast path: add another copy of a known work by its DB id.
        try:
            wid = int(copy_work_id.strip())
            item = svc.add_item_to_work(
                wid,
                location=location.strip() or None,
                call_number=call_number.strip() or None,
                condition=condition.strip() or None,
            )
        except (BusinessRuleError, NotFoundError, ValidationError) as exc:
            return _render(
                "items/new.html",
                request,
                {"request": request, "user": user, "error": str(exc),
                 "preset_media_type": "book", "copy_work": None,
                 "added_barcode": None, "added_title": None, "added_work_id": None},
            )
        return RedirectResponse(f"/ui/catalog/{wid}?message=Copy+added.", status_code=303)
    try:
        work, item = svc.add_from_lookup(
            media_type.strip(),
            identifier_kind.strip(),
            identifier_value.strip(),
            location=location.strip() or None,
        )
    except (BusinessRuleError, NotFoundError, ExternalLookupError, ValidationError) as exc:
        return _render(
            "items/new.html",
            request,
            {"request": request, "user": user, "error": str(exc),
             "preset_media_type": media_type.strip() or "book",
             "copy_work": None, "added_barcode": None, "added_title": None, "added_work_id": None},
        )
    if call_number.strip():
        item.call_number = call_number.strip()
    if condition.strip():
        item.condition = condition.strip()
    mt = media_type.strip()
    return RedirectResponse(
        f"/ui/items/new?media_type={quote(mt)}&added={quote(item.barcode)}&work_id={work.id}",
        status_code=303,
    )


@router.get("/items/{barcode}")
def item_detail(
    barcode: str,
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
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
    from compendium.repositories.sql.loan_repository import SqlLoanRepository
    from compendium.services.auth import has_permission

    loan_history: list = []
    if has_permission(user.role.permissions, "loan.view.any"):
        loan_history = SqlLoanRepository(session).list_for_item(item.id, limit=25)
    note_entries = SqlItemNoteRepository(session).list_for_item(item.id)
    return _render(
        "items/detail.html",
        request,
        {
            "request": request,
            "user": user,
            "item": item,
            "loan_history": loan_history,
            "note_entries": note_entries,
            "message": message,
            "error": error,
        },
    )


@router.get("/items/{barcode}/edit")
def item_edit_form(
    barcode: str,
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM_EDIT)),
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
        "items/edit.html",
        request,
        {"request": request, "user": user, "item": item, "error": None},
    )


@router.post("/items/{barcode}/edit")
def item_edit_submit(
    barcode: str,
    request: Request,
    location: str = Form(default=""),
    call_number: str = Form(default=""),
    condition: str = Form(default=""),
    notes: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM_EDIT)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _catalog_svc(session, user).update_item(
            barcode,
            location=location,
            call_number=call_number,
            condition=condition,
            notes=notes,
        )
    except NotFoundError:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"Item '{barcode}' not found"},
            status_code=404,
        )
    except (BusinessRuleError, ValidationError) as exc:
        item = SqlItemRepository(session).get_by_barcode(barcode)
        return _render(
            "items/edit.html",
            request,
            {"request": request, "user": user, "item": item, "error": str(exc)},
        )
    return RedirectResponse(f"/ui/items/{barcode}?message=Item+updated.", status_code=303)


@router.get("/items/{barcode}/loanable")
def item_loanable_form(
    barcode: str,
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM_EDIT)),
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
        "items/loanable.html",
        request,
        {
            "request": request,
            "user": user,
            "item": item,
            "REASON_CHOICES": _REASON_CHOICES,
            "error": None,
        },
    )


@router.post("/items/{barcode}/loanable")
def item_loanable_submit(
    barcode: str,
    request: Request,
    is_loanable: str = Form(default="yes"),
    reason: str = Form(default=""),
    note: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM_EDIT)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    flag = is_loanable.strip().lower() == "yes"
    try:
        _catalog_svc(session, user).set_loanable(
            barcode,
            is_loanable=flag,
            reason=reason.strip() or None,
            note=note.strip() or None,
        )
    except NotFoundError:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"Item '{barcode}' not found"},
            status_code=404,
        )
    except (BusinessRuleError, ValidationError) as exc:
        item = SqlItemRepository(session).get_by_barcode(barcode)
        return _render(
            "items/loanable.html",
            request,
            {
                "request": request,
                "user": user,
                "item": item,
                "REASON_CHOICES": _REASON_CHOICES,
                "error": str(exc),
            },
        )
    return RedirectResponse(
        f"/ui/items/{barcode}?message=Loan+status+updated.", status_code=303
    )


@router.get("/items/{barcode}/withdraw-confirm")
def withdraw_confirm_form(
    barcode: str,
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM_MANAGE)),
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
    if item.status == "withdrawn":
        return RedirectResponse(f"/ui/items/{barcode}", status_code=303)
    return _render(
        "items/withdraw_confirm.html",
        request,
        {"request": request, "user": user, "item": item},
    )


@router.post("/items/{barcode}/withdraw")
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
    except NotFoundError:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"Item '{barcode}' not found"},
            status_code=404,
        )
    except BusinessRuleError as exc:
        return RedirectResponse(
            f"/ui/items/{barcode}?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(
        f"/ui/items/{barcode}?message=Item+withdrawn.", status_code=303
    )


@router.post("/items/{barcode}/notes/add")
def add_item_note(
    barcode: str,
    request: Request,
    kind: str = Form(default="general"),
    note: str = Form(...),
    event_date: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM_EDIT)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    from datetime import date as date_type

    parsed_date: date_type | None = None
    if event_date.strip():
        try:
            parsed_date = date_type.fromisoformat(event_date.strip())
        except ValueError:
            return RedirectResponse(
                f"/ui/items/{barcode}?error={quote('Invalid date format.')}",
                status_code=303,
            )
    try:
        _note_svc(session, user).add_note(
            barcode, kind=kind, note=note, event_date=parsed_date
        )
    except (ValidationError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/items/{barcode}?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(f"/ui/items/{barcode}?message=Note+added.", status_code=303)


@router.post("/items/{barcode}/notes/{note_id}/delete")
def delete_item_note(
    barcode: str,
    note_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM_EDIT)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _note_svc(session, user).delete_note(barcode, note_id)
    except (BusinessRuleError, NotFoundError) as exc:
        return RedirectResponse(
            f"/ui/items/{barcode}?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(f"/ui/items/{barcode}?message=Note+deleted.", status_code=303)
