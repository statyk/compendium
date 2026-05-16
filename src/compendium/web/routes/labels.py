"""Web UI for label + patron-card PDF generation."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.services.site_settings import get_site_setting
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Item, MediaType, Patron, PatronCategory, Work
from compendium.services.labels import (
    ItemLabelRow,
    PatronCardRow,
    TEMPLATES,
    generate_item_labels,
    generate_patron_cards,
)
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "labels.generate"

# Templates suitable for patron cards: exclude rotated-orientation templates
# (spine-rotated labels are for item spines only and don't render patron cards).
_PATRON_TEMPLATES = [t for t in TEMPLATES.values() if t.orientation != "rotated"]

_SYMBOLOGY_LABELS = {
    "codabar": "Codabar",
    "code39": "Code 39",
    "code128": "Code 128",
}


def _symbology_ctx() -> dict:
    """Render context for the active barcode symbology, surfaced as a
    banner on the label-form pages so operators see what their PDFs
    will use without having to navigate to the settings page first."""
    code = get_site_setting("barcode_symbology")
    return {
        "barcode_symbology": code,
        "barcode_symbology_label": _SYMBOLOGY_LABELS.get(code, code),
    }


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    # Every label-form page renders the active-symbology banner; inject
    # the values here so each render-call site doesn't have to remember.
    ctx_clean.setdefault("barcode_symbology", None)
    ctx_clean.setdefault("barcode_symbology_label", None)
    sym = _symbology_ctx()
    ctx_clean["barcode_symbology"] = sym["barcode_symbology"]
    ctx_clean["barcode_symbology_label"] = sym["barcode_symbology_label"]
    resp = templates.TemplateResponse(request, name, ctx_clean, status_code=status_code)
    if fresh:
        set_csrf_cookie(resp, fresh)
    return resp


def _csv(s: str) -> list[str] | None:
    parts = [x.strip() for x in s.split(",") if x.strip()]
    return parts or None


def _collect_item_rows(
    session: Session,
    *,
    barcodes: list[str] | None,
    branch_code: str | None,
    media_type_code: str | None,
    since: str | None,
) -> list[ItemLabelRow]:
    from compendium.domain.models import Branch

    q = session.query(Item).join(Item.work)
    if barcodes:
        q = q.filter(Item.barcode.in_(barcodes))
    if branch_code:
        q = q.join(Item.branch).filter(Branch.code == branch_code)
    if media_type_code:
        q = q.join(Work.media_type).filter(MediaType.code == media_type_code)
    if since:
        try:
            cutoff = datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            return []
        q = q.filter(Item.created_at >= cutoff)
    q = q.order_by(Item.accession_number)
    rows: list[ItemLabelRow] = []
    for item in q.all():
        author = ""
        if item.work.creators:
            author = item.work.creators[0].creator.display_name
        rows.append(
            ItemLabelRow(
                barcode=item.barcode,
                title=item.work.title,
                author_display=author,
                call_number=item.call_number,
                publication_year=item.work.publication_year,
                isbn=item.work.isbn,
                branch_code=item.branch.code if item.branch else None,
                location=item.location,
            )
        )
    return rows


def _collect_patron_rows(
    session: Session,
    *,
    card_numbers: list[str] | None,
    category_code: str | None,
    active_only: bool,
) -> list[PatronCardRow]:
    q = session.query(Patron)
    if card_numbers:
        q = q.filter(Patron.library_card_number.in_(card_numbers))
    if category_code:
        q = q.join(Patron.category).filter(
            PatronCategory.code == category_code.lower()
        )
    if active_only:
        q = q.filter(Patron.is_active.is_(True))
    q = q.order_by(Patron.library_card_number)
    return [
        PatronCardRow(
            card_number=p.library_card_number,
            full_name=p.full_name,
            expires_at=p.expires_at,
        )
        for p in q.all()
    ]


def _branches(session: Session):
    from compendium.repositories.sql.branch_repository import SqlBranchRepository

    return SqlBranchRepository(session).list()


def _media_types(session: Session):
    return session.query(MediaType).order_by(MediaType.display_name).all()


def _categories(session: Session):
    from compendium.repositories.sql.patron_category_repository import (
        SqlPatronCategoryRepository,
    )

    return SqlPatronCategoryRepository(session).list()


def _pdf_response(pdf: bytes, filename: str) -> Response:
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/labels")
def labels_index(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
):
    return _render(
        "labels/index.html",
        request,
        {"request": request, "user": user, "templates": list(TEMPLATES.values())},
    )


@router.get("/labels/items")
def item_labels_form(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    return _render(
        "labels/items.html",
        request,
        {
            "request": request,
            "user": user,
            "templates": list(TEMPLATES.values()),
            "branches": _branches(session),
            "media_types": _media_types(session),
            "error": None,
        },
    )


@router.post("/labels/items")
def item_labels_post(
    request: Request,
    template: str = Form("avery-5160"),
    format: str = Form(""),
    use_isbn_barcode: str = Form(""),
    branch: str = Form(""),
    media_type: str = Form(""),
    since: str = Form(""),
    barcodes: str = Form(""),
    start_label: int = Form(0),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    if template not in TEMPLATES:
        return _render(
            "labels/items.html",
            request,
            {
                "request": request,
                "user": user,
                "templates": list(TEMPLATES.values()),
                "branches": _branches(session),
                "media_types": _media_types(session),
                "error": f"Unknown template '{template}'",
            },
        )
    rows = _collect_item_rows(
        session,
        barcodes=_csv(barcodes),
        branch_code=branch or None,
        media_type_code=media_type or None,
        since=since or None,
    )
    if not rows:
        return _render(
            "labels/items.html",
            request,
            {
                "request": request,
                "user": user,
                "templates": list(TEMPLATES.values()),
                "branches": _branches(session),
                "media_types": _media_types(session),
                "error": "No items matched the filter.",
            },
        )
    pdf = generate_item_labels(
        rows,
        template_key=template,
        format=(format or None),
        use_isbn_barcode=(use_isbn_barcode == "on"),
        start_label=max(0, int(start_label or 0)),
    )
    return _pdf_response(pdf, "item-labels.pdf")


@router.get("/labels/patrons")
def patron_cards_form(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    return _render(
        "labels/patrons.html",
        request,
        {
            "request": request,
            "user": user,
            "templates": _PATRON_TEMPLATES,
            "categories": _categories(session),
            "error": None,
        },
    )


@router.post("/labels/patrons")
def patron_cards_post(
    request: Request,
    template: str = Form("avery-5871"),
    format: str = Form("full"),
    cards: str = Form(""),
    category: str = Form(""),
    active_only: str = Form(""),
    start_label: int = Form(0),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    _patron_template_keys = {t.key for t in _PATRON_TEMPLATES}
    if template not in _patron_template_keys:
        return _render(
            "labels/patrons.html",
            request,
            {
                "request": request,
                "user": user,
                "templates": _PATRON_TEMPLATES,
                "categories": _categories(session),
                "error": f"Unknown template '{template}'",
            },
        )
    if format not in ("full", "sticker"):
        format = "full"
    rows = _collect_patron_rows(
        session,
        card_numbers=_csv(cards),
        category_code=category or None,
        active_only=(active_only == "on"),
    )
    if not rows:
        return _render(
            "labels/patrons.html",
            request,
            {
                "request": request,
                "user": user,
                "templates": _PATRON_TEMPLATES,
                "categories": _categories(session),
                "error": "No patrons matched the filter.",
            },
        )
    try:
        pdf = generate_patron_cards(
            rows,
            template_key=template,
            format=format,
            library_name=get_site_setting("library_name"),
            start_label=max(0, int(start_label or 0)),
        )
    except ValueError as exc:
        return _render(
            "labels/patrons.html",
            request,
            {
                "request": request,
                "user": user,
                "templates": _PATRON_TEMPLATES,
                "categories": _categories(session),
                "error": str(exc),
            },
        )
    return _pdf_response(pdf, "patron-cards.pdf")
