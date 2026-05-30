from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import CreateItemNoteRequest, ItemDetail, ItemNoteResponse, ItemUpdate, LoanableUpdate
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.repositories.sql.counters import SqlCounterRepository
from compendium.services.catalog import CatalogService

router = APIRouter()


def _catalog(session: Session, actor: AppUser | None = None) -> CatalogService:
    from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
    from compendium.repositories.sql.hold_repository import SqlHoldRepository
    from compendium.services.audit import AuditService

    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
        hold_repo=SqlHoldRepository(session),
        counter_repo=SqlCounterRepository(session),
        item_note_repo=SqlItemNoteRepository(session),
    )


@router.get("/{barcode}", response_model=ItemDetail)
def get_item(
    barcode: str,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("item.view")),
) -> ItemDetail:
    item = SqlItemRepository(session).get_by_barcode(barcode)
    if item is None:
        raise HTTPException(status_code=404, detail=f"No item with barcode '{barcode}'")
    return ItemDetail.model_validate(item)


@router.post("/{barcode}/withdraw", response_model=ItemDetail)
def withdraw_item(
    barcode: str,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("item.delete")),
) -> ItemDetail:
    try:
        item = _catalog(session, user).withdraw_item(barcode)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ItemDetail.model_validate(item)


@router.patch("/{barcode}", response_model=ItemDetail)
def update_item(
    barcode: str,
    payload: ItemUpdate,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("item.edit")),
) -> ItemDetail:
    kwargs = payload.model_dump(include=payload.model_fields_set)
    try:
        item = _catalog(session, user).update_item(barcode, **kwargs)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (BusinessRuleError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ItemDetail.model_validate(item)


@router.post("/{barcode}/loanable", response_model=ItemDetail)
def set_loanable(
    barcode: str,
    payload: LoanableUpdate,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("item.edit")),
) -> ItemDetail:
    try:
        item = _catalog(session, user).set_loanable(
            barcode,
            is_loanable=payload.is_loanable,
            reason=payload.reason,
            note=payload.note,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (BusinessRuleError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ItemDetail.model_validate(item)


def _note_svc(session: Session, actor: AppUser):
    from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
    from compendium.services.audit import AuditService
    from compendium.services.item_notes import ItemNoteService

    return ItemNoteService(
        item_note_repo=SqlItemNoteRepository(session),
        item_repo=SqlItemRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
    )


@router.get("/{barcode}/notes", response_model=list[ItemNoteResponse])
def list_item_notes(
    barcode: str,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("item.view")),
) -> list[ItemNoteResponse]:
    try:
        notes = _note_svc(session, _user).list_for_item(barcode)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [ItemNoteResponse.model_validate(n) for n in notes]


@router.post("/{barcode}/notes", response_model=ItemNoteResponse, status_code=201)
def create_item_note(
    barcode: str,
    payload: CreateItemNoteRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("item.edit")),
) -> ItemNoteResponse:
    try:
        note = _note_svc(session, user).add_note(
            barcode,
            kind=payload.kind,
            note=payload.note,
            event_date=payload.event_date,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValidationError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ItemNoteResponse.model_validate(note)


@router.delete("/{barcode}/notes/{note_id}", status_code=204)
def delete_item_note(
    barcode: str,
    note_id: int,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("item.edit")),
) -> None:
    try:
        _note_svc(session, user).delete_note(barcode, note_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
