"""Web UI for label + patron-card PDF generation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from compendium.services.site_settings import get_site_setting
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Item, MediaType, Patron, PatronCategory, Work
from compendium.services.labels import (
    DEFAULT_FIELDS,
    ITEM_KIND_TO_FORMAT,
    KIND_DEFAULT_TEMPLATE,
    OPTIONAL_FIELDS,
    PATRON_KIND_TO_FORMAT,
    ItemLabelRow,
    PatronCardRow,
    TEMPLATES,
    compatible_templates,
    generate_item_labels,
    generate_patron_cards,
    render_item_label_svg,
)

from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "labels.generate"

_SYMBOLOGY_LABELS = {
    "codabar": "Codabar",
    "code39": "Code 39",
    "code128": "Code 128",
}

# Map kind → admin setting key that stores the default field list.
_KIND_SETTING: dict[str, str] = {
    "spine":          "label_spine_default_fields",
    "pocket":         "label_pocket_default_fields",
    "barcode-only":   "label_barcode_only_default_fields",
    "patron-full":    "label_patron_full_default_fields",
    "patron-sticker": "label_patron_sticker_default_fields",
}

# Human-readable labels for each kind shown in the form's field-toggle UI.
ITEM_KIND_LABELS: dict[str, str] = {
    "spine":        "Spine label",
    "pocket":       "Pocket label",
    "barcode-only": "Barcode-only sticker",
}

PATRON_KIND_LABELS: dict[str, str] = {
    "patron-full":    "Full card (library name + patron info + barcode)",
    "patron-sticker": "Sticker (barcode only — affix to pre-printed card)",
}

# Human-readable display names for each field token.
FIELD_DISPLAY_NAMES: dict[str, str] = {
    "location": "Location section (e.g. REFERENCE)",
    "branch": "Branch code",
    "cutter": "Cutter (author indicator)",
    "year": "Publication year",
    "title": "Title",
    "author": "Author",
    "call_number": "Call number",
    "human_readable": "Human-readable digits below barcode",
    "library_name": "Library name header",
    "subtitle": '"Library Card" subtitle',
    "patron_name": "Patron name",
    "expiry": "Expiry date",
    "category": "Patron category",
    "card_number": "Card number below barcode",
    "barcode": "Barcode",
}


def _symbology_ctx() -> dict:
    code = get_site_setting("barcode_symbology")
    return {
        "barcode_symbology": code,
        "barcode_symbology_label": _SYMBOLOGY_LABELS.get(code, code),
    }


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
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


def _default_fields_for_kind(kind: str) -> frozenset[str]:
    """Return the admin-configured (or code-default) field set for a kind."""
    setting_key = _KIND_SETTING.get(kind)
    if setting_key:
        try:
            stored = get_site_setting(setting_key)
            if isinstance(stored, list):
                return frozenset(stored)
        except Exception:
            pass
    fmt = ITEM_KIND_TO_FORMAT.get(kind) or PATRON_KIND_TO_FORMAT.get(kind, kind)
    return DEFAULT_FIELDS.get(fmt, frozenset())


def _fields_context_for_kind(kind: str) -> list[dict[str, Any]]:
    """Build the per-field checkbox data for a given kind."""
    fmt = ITEM_KIND_TO_FORMAT.get(kind) or PATRON_KIND_TO_FORMAT.get(kind, kind)
    optional = OPTIONAL_FIELDS.get(fmt, frozenset())
    defaults = _default_fields_for_kind(kind)
    result = []
    for f in sorted(optional):
        result.append({
            "name": f,
            "label": FIELD_DISPLAY_NAMES.get(f, f),
            "checked_by_default": f in defaults,
        })
    return result


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


def _count_item_rows(
    session: Session,
    *,
    barcodes: list[str] | None,
    branch_code: str | None,
    media_type_code: str | None,
    since: str | None,
) -> int:
    """Cheap count for the 'N items match' preview."""
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
            return 0
        q = q.filter(Item.created_at >= cutoff)
    return q.count()


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
            category_display=p.category.display_name if p.category else None,
        )
        for p in q.all()
    ]


def _count_patron_rows(
    session: Session,
    *,
    card_numbers: list[str] | None,
    category_code: str | None,
    active_only: bool,
) -> int:
    q = session.query(Patron)
    if card_numbers:
        q = q.filter(Patron.library_card_number.in_(card_numbers))
    if category_code:
        q = q.join(Patron.category).filter(PatronCategory.code == category_code.lower())
    if active_only:
        q = q.filter(Patron.is_active.is_(True))
    return q.count()


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


def _item_form_ctx(
    session: Session,
    kind: str,
    item_count: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the template context for the item-labels form."""
    all_item_kinds = {
        k: {
            "label": ITEM_KIND_LABELS[k],
            "templates": compatible_templates(k),
            "default_template": KIND_DEFAULT_TEMPLATE.get(k, "avery-5160"),
            "fields": _fields_context_for_kind(k),
        }
        for k in ITEM_KIND_TO_FORMAT
    }
    return {
        "item_kinds": all_item_kinds,
        "item_kind_order": list(ITEM_KIND_TO_FORMAT.keys()),
        "selected_kind": kind,
        "branches": _branches(session),
        "media_types": _media_types(session),
        "item_count": item_count,
        "error": error,
    }


def _patron_form_ctx(
    session: Session,
    kind: str,
    patron_count: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the template context for the patron-cards form."""
    all_patron_kinds = {
        k: {
            "label": PATRON_KIND_LABELS[k],
            "templates": compatible_templates(k),
            "default_template": KIND_DEFAULT_TEMPLATE.get(k, "avery-5871"),
            "fields": _fields_context_for_kind(k),
        }
        for k in PATRON_KIND_TO_FORMAT
    }
    return {
        "patron_kinds": all_patron_kinds,
        "patron_kind_order": list(PATRON_KIND_TO_FORMAT.keys()),
        "selected_kind": kind,
        "categories": _categories(session),
        "patron_count": patron_count,
        "error": error,
    }


def _preview_context(kind: str, template_key: str, fields: frozenset[str]) -> dict[str, Any]:
    """Build context for the label preview fragment."""
    if kind not in ITEM_KIND_TO_FORMAT:
        kind = "pocket"
    compatible = [t.key for t in compatible_templates(kind)]
    if template_key not in compatible:
        fallback = compatible[0] if compatible else "avery-5160"
        template_key = KIND_DEFAULT_TEMPLATE.get(kind, fallback)
    if not fields:
        fields = DEFAULT_FIELDS.get(ITEM_KIND_TO_FORMAT.get(kind, kind), frozenset())
    symbology = get_site_setting("barcode_symbology")
    svg = render_item_label_svg(
        kind=kind, template_key=template_key, fields=fields, symbology=symbology
    )
    return {"svg": svg}


@router.get("/labels/items/preview", response_class=Response)
def item_labels_preview(
    request: Request,
    kind: str = "pocket",
    template: str = "",
    user: AppUser = Depends(require_web_permission(_PERM)),
):
    fields = frozenset(
        name[len("field_"):]
        for name in request.query_params.keys()
        if name.startswith("field_")
    )
    ctx = _preview_context(kind, template, fields)
    ctx["request"] = request
    ctx["user"] = user
    return _render("labels/_label_preview.html", request, ctx)


@router.get("/labels")
def labels_index(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
):
    return _render(
        "labels/index.html",
        request,
        {"request": request, "user": user},
    )


@router.get("/labels/items")
def item_labels_form(
    request: Request,
    kind: str = "pocket",
    branch: str = "",
    media_type: str = "",
    since: str = "",
    barcodes: str = "",
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    if kind not in ITEM_KIND_TO_FORMAT:
        kind = "pocket"
    item_count = _count_item_rows(
        session,
        barcodes=_csv(barcodes) if barcodes else None,
        branch_code=branch or None,
        media_type_code=media_type or None,
        since=since or None,
    )
    ctx = _item_form_ctx(session, kind, item_count=item_count)
    ctx["request"] = request
    ctx["user"] = user
    # Carry filter values back so the form is pre-populated.
    ctx["filter_branch"] = branch
    ctx["filter_media_type"] = media_type
    ctx["filter_since"] = since
    ctx["filter_barcodes"] = barcodes
    return _render("labels/items.html", request, ctx)


@router.post("/labels/items")
async def item_labels_post(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))

    kind = str(form.get("kind", "pocket"))
    if kind not in ITEM_KIND_TO_FORMAT:
        return _render(
            "labels/items.html",
            request,
            {
                "request": request, "user": user,
                **_item_form_ctx(session, "pocket", error=f"Unknown label kind '{kind}'"),
                "filter_branch": "", "filter_media_type": "", "filter_since": "", "filter_barcodes": "",
            },
            status_code=400,
        )

    fmt = ITEM_KIND_TO_FORMAT[kind]
    template_key = str(form.get("template", KIND_DEFAULT_TEMPLATE.get(kind, "avery-5160")))
    if template_key not in TEMPLATES:
        template_key = KIND_DEFAULT_TEMPLATE.get(kind, "avery-5160")

    use_isbn_barcode = form.get("use_isbn_barcode", "") == "on"
    start_label = max(0, int(form.get("start_label", 0) or 0))

    branch = str(form.get("branch", ""))
    media_type = str(form.get("media_type", ""))
    since = str(form.get("since", ""))
    barcodes_raw = str(form.get("barcodes", ""))

    # Build selected fields: checked checkboxes + required fields.
    optional_for_kind = OPTIONAL_FIELDS.get(fmt, frozenset())
    selected_optional: set[str] = set()
    for f in optional_for_kind:
        if form.get(f"field_{f}") == "on":
            selected_optional.add(f)
    fields = frozenset(selected_optional)

    rows = _collect_item_rows(
        session,
        barcodes=_csv(barcodes_raw),
        branch_code=branch or None,
        media_type_code=media_type or None,
        since=since or None,
    )
    if not rows:
        item_count = 0
        ctx = _item_form_ctx(session, kind, item_count=item_count, error="No items matched the filter.")
        ctx["request"] = request
        ctx["user"] = user
        ctx["filter_branch"] = branch
        ctx["filter_media_type"] = media_type
        ctx["filter_since"] = since
        ctx["filter_barcodes"] = barcodes_raw
        return _render("labels/items.html", request, ctx)

    pdf = generate_item_labels(
        rows,
        template_key=template_key,
        format=fmt,
        use_isbn_barcode=use_isbn_barcode,
        start_label=start_label,
        fields=fields,
        library_name=get_site_setting("library_name"),
    )
    return _pdf_response(pdf, "item-labels.pdf")


@router.get("/labels/patrons")
def patron_cards_form(
    request: Request,
    kind: str = "patron-full",
    cards: str = "",
    category: str = "",
    active_only: str = "",
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    if kind not in PATRON_KIND_TO_FORMAT:
        kind = "patron-full"
    patron_count = _count_patron_rows(
        session,
        card_numbers=_csv(cards) if cards else None,
        category_code=category or None,
        active_only=(active_only == "on"),
    )
    ctx = _patron_form_ctx(session, kind, patron_count=patron_count)
    ctx["request"] = request
    ctx["user"] = user
    ctx["filter_cards"] = cards
    ctx["filter_category"] = category
    ctx["filter_active_only"] = active_only == "on"
    return _render("labels/patrons.html", request, ctx)


@router.post("/labels/patrons")
async def patron_cards_post(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))

    kind = str(form.get("kind", "patron-full"))
    if kind not in PATRON_KIND_TO_FORMAT:
        kind = "patron-full"

    fmt = PATRON_KIND_TO_FORMAT[kind]
    template_key = str(form.get("template", KIND_DEFAULT_TEMPLATE.get(kind, "avery-5871")))
    if template_key not in TEMPLATES:
        template_key = KIND_DEFAULT_TEMPLATE.get(kind, "avery-5871")

    start_label = max(0, int(form.get("start_label", 0) or 0))
    cards_raw = str(form.get("cards", ""))
    category = str(form.get("category", ""))
    active_only = form.get("active_only", "") == "on"

    optional_for_kind = OPTIONAL_FIELDS.get(fmt, frozenset())
    selected_optional: set[str] = set()
    for f in optional_for_kind:
        if form.get(f"field_{f}") == "on":
            selected_optional.add(f)
    fields = frozenset(selected_optional)

    rows = _collect_patron_rows(
        session,
        card_numbers=_csv(cards_raw),
        category_code=category or None,
        active_only=active_only,
    )
    if not rows:
        ctx = _patron_form_ctx(session, kind, patron_count=0, error="No patrons matched the filter.")
        ctx["request"] = request
        ctx["user"] = user
        ctx["filter_cards"] = cards_raw
        ctx["filter_category"] = category
        ctx["filter_active_only"] = active_only
        return _render("labels/patrons.html", request, ctx)

    try:
        pdf = generate_patron_cards(
            rows,
            template_key=template_key,
            format=fmt,
            library_name=get_site_setting("library_name"),
            start_label=start_label,
            fields=fields,
        )
    except ValueError as exc:
        ctx = _patron_form_ctx(session, kind, error=str(exc))
        ctx["request"] = request
        ctx["user"] = user
        ctx["filter_cards"] = cards_raw
        ctx["filter_category"] = category
        ctx["filter_active_only"] = active_only
        return _render("labels/patrons.html", request, ctx)

    return _pdf_response(pdf, "patron-cards.pdf")
