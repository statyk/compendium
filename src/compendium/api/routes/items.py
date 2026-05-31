from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import (
    CreateItemNoteRequest,
    DeclareLostRequest,
    ItemDetail,
    ItemNoteResponse,
    ItemUpdate,
    LoanableUpdate,
    MarkDamagedRequest,
)
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.repositories.sql.counters import SqlCounterRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
from compendium.services.fines import FineService
from compendium.services.site_settings import get_site_setting

router = APIRouter()


def _fine_svc(session: Session, user: AppUser | None) -> FineService:
    return FineService(
        fine_repo=SqlFineRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        settings=get_settings(),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=user,
        source="api",
    )


def _circulation(session: Session, user: AppUser | None) -> CirculationService:
    settings = get_settings()
    audit = AuditService(SqlAuditLogRepository(session))
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        hold_pickup_days=get_site_setting("hold_pickup_days"),
        fine_svc=_fine_svc(session, user),
        audit_svc=audit,
        actor=user,
        source="api",
        item_note_repo=SqlItemNoteRepository(session),
    )


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


# ── item state transitions ────────────────────────────────────────────────────


@router.post("/{barcode}/lost", response_model=ItemDetail)
def declare_lost(
    barcode: str,
    body: DeclareLostRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("fine.manage")),
) -> ItemDetail:
    try:
        item = _circulation(session, user).declare_lost(
            barcode,
            replacement_cost_cents=body.replacement_cost_cents,
            note=body.note,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValidationError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ItemDetail.model_validate(item)


@router.post("/{barcode}/damaged", response_model=ItemDetail)
def mark_damaged(
    barcode: str,
    body: MarkDamagedRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("fine.manage")),
) -> ItemDetail:
    try:
        item = _circulation(session, user).mark_damaged(
            barcode, amount_cents=body.amount_cents, note=body.note
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValidationError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ItemDetail.model_validate(item)


@router.post("/{barcode}/clear-damage", response_model=ItemDetail)
def clear_damage(
    barcode: str,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("fine.manage")),
) -> ItemDetail:
    try:
        item = _circulation(session, user).clear_damage(barcode)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ItemDetail.model_validate(item)


@router.post("/{barcode}/clear-lost", response_model=ItemDetail)
def clear_lost(
    barcode: str,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("fine.manage")),
) -> ItemDetail:
    try:
        item = _circulation(session, user).clear_lost(barcode)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ItemDetail.model_validate(item)


@router.post("/{barcode}/verify-returned", response_model=ItemDetail)
def verify_returned(
    barcode: str,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("loan.checkin")),
) -> ItemDetail:
    try:
        _circulation(session, user).verify_returned(barcode)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    item = SqlItemRepository(session).get_by_barcode(barcode)
    return ItemDetail.model_validate(item)


class _WriteOffClaimRequest(BaseModel):
    note: str


@router.post("/{barcode}/write-off-claim", response_model=ItemDetail)
def write_off_claim(
    barcode: str,
    body: _WriteOffClaimRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("loan.checkin")),
) -> ItemDetail:
    try:
        _circulation(session, user).write_off_claim(barcode, note=body.note)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (BusinessRuleError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    item = SqlItemRepository(session).get_by_barcode(barcode)
    return ItemDetail.model_validate(item)
