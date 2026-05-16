"""REST endpoints for label + patron-card PDF generation."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Item, MediaType, Patron, PatronCategory, Work
from compendium.services.labels import (
    ItemLabelRow,
    PatronCardRow,
    TEMPLATES,
    generate_item_labels,
    generate_patron_cards,
)
from compendium.services.site_settings import get_site_setting

router = APIRouter()

_PERM = "labels.generate"


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
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="since must be YYYY-MM-DD") from exc
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
                location=item.location if item.location else None,
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


def _pdf_response(pdf: bytes, filename: str) -> Response:
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv(s: str | None) -> list[str] | None:
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


@router.get("/items")
def item_labels(
    template: str = Query("avery-5160"),
    format: str | None = Query(None, pattern="^(spine|spine-text|spine-barcode|pocket|barcode-only)$"),
    use_isbn_barcode: bool = False,
    branch: str | None = None,
    media_type: str | None = None,
    since: str | None = None,
    barcodes: str | None = None,
    start_label: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission(_PERM)),
):
    if template not in TEMPLATES:
        raise HTTPException(status_code=400, detail=f"Unknown template '{template}'")
    rows = _collect_item_rows(
        session,
        barcodes=_csv(barcodes),
        branch_code=branch,
        media_type_code=media_type,
        since=since,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No items matched the filter")
    pdf = generate_item_labels(
        rows,
        template_key=template,
        format=format,
        use_isbn_barcode=use_isbn_barcode,
        start_label=start_label,
    )
    return _pdf_response(pdf, "item-labels.pdf")


@router.get("/patrons")
def patron_cards(
    template: str = Query("avery-5871"),
    format: str = Query("full", pattern="^(full|sticker)$"),
    cards: str | None = None,
    category: str | None = None,
    active_only: bool = False,
    start_label: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission(_PERM)),
):
    if template not in TEMPLATES:
        raise HTTPException(status_code=400, detail=f"Unknown template '{template}'")
    rows = _collect_patron_rows(
        session,
        card_numbers=_csv(cards),
        category_code=category,
        active_only=active_only,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No patrons matched the filter")
    try:
        pdf = generate_patron_cards(
            rows,
            template_key=template,
            format=format,
            library_name=get_site_setting("library_name"),
            start_label=start_label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _pdf_response(pdf, "patron-cards.pdf")
