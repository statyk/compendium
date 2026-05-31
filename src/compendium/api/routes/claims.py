from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import ItemDetail, LoanResponse
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
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


class ClaimsListRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    loan_id: int
    barcode: str
    title: str
    patron_card: str
    patron_name: str


class _WriteOffClaimRequest(BaseModel):
    note: str


@router.get("", response_model=list[ClaimsListRow])
def list_claims(
    limit: int = 200,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("loan.checkin")),
) -> list[ClaimsListRow]:
    from compendium.domain.enums import ItemStatus
    from compendium.domain.models import Item, Loan, Patron, Work

    rows = (
        session.query(Loan, Item, Work, Patron)
        .join(Item, Loan.item_id == Item.id)
        .join(Work, Item.work_id == Work.id)
        .join(Patron, Loan.patron_id == Patron.id)
        .filter(
            Loan.returned_at.is_(None),
            Item.status == ItemStatus.CLAIMS_RETURNED.value,
        )
        .order_by(Loan.id)
        .limit(min(limit, 500))
        .all()
    )
    return [
        ClaimsListRow(
            loan_id=loan.id,
            barcode=item.barcode,
            title=work.title,
            patron_card=patron.library_card_number,
            patron_name=patron.full_name,
        )
        for (loan, item, work, patron) in rows
    ]


@router.post("/{barcode}/returned", response_model=LoanResponse)
def claim_returned(
    barcode: str,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("loan.checkin")),
) -> LoanResponse:
    """Librarian-initiated claim. Patron access via /me/loans/{id}/claim-returned."""
    try:
        item = _circulation(session, user).claim_returned(barcode)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Service marks item CLAIMS_RETURNED; re-fetch the active loan to return
    loan = SqlLoanRepository(session).get_active_for_item(item.id)
    if loan is None:
        raise HTTPException(status_code=404, detail=f"No active loan for barcode '{barcode}'")
    return LoanResponse.model_validate(loan)


@router.post("/{barcode}/verify", response_model=ItemDetail)
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


@router.post("/{barcode}/write-off", response_model=ItemDetail)
def write_off_claim(
    barcode: str,
    body: _WriteOffClaimRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("loan.checkin")),
) -> ItemDetail:
    from compendium.domain.errors import ValidationError

    try:
        _circulation(session, user).write_off_claim(barcode, note=body.note)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (BusinessRuleError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    item = SqlItemRepository(session).get_by_barcode(barcode)
    return ItemDetail.model_validate(item)
