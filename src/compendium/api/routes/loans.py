from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import CheckoutRequest, LoanResponse
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.circulation import CirculationService

router = APIRouter()


def _circulation(session: Session, actor: AppUser | None = None) -> CirculationService:
    settings = get_settings()
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        hold_pickup_days=settings.hold_pickup_days,
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
    )


@router.post("/checkout", status_code=201, response_model=LoanResponse)
def checkout(
    body: CheckoutRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("loan.checkout")),
) -> LoanResponse:
    try:
        loan = _circulation(session, actor=user).checkout(
            body.barcode, body.card_number, override_holds=body.override_holds
        )
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LoanResponse.model_validate(loan)


@router.post("/{loan_id}/checkin", response_model=LoanResponse)
def checkin(
    loan_id: int = Path(),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("loan.checkin")),
) -> LoanResponse:
    try:
        loan = _circulation(session).checkin_by_id(loan_id)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LoanResponse.model_validate(loan)


@router.post("/{loan_id}/renew", response_model=LoanResponse)
def renew(
    loan_id: int = Path(),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("loan.renew.any")),
) -> LoanResponse:
    try:
        loan = _circulation(session).renew_by_id(loan_id)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LoanResponse.model_validate(loan)


# ── Claims-returned ─────────────────────────────────────────────────────────


class ClaimsListRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    loan_id: int
    barcode: str
    title: str
    patron_card: str
    patron_name: str


@router.get("/claims", response_model=list[ClaimsListRow])
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


@router.post("/{loan_id}/claim-returned", response_model=LoanResponse)
def claim_returned(
    loan_id: int = Path(),
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("loan.checkin")),
) -> LoanResponse:
    """Librarian-initiated claim (patron has access via /me/loans/{id}/claim-returned)."""
    loan = SqlLoanRepository(session).get(loan_id)
    if loan is None:
        raise HTTPException(status_code=404, detail=f"No loan with id={loan_id}")
    item = SqlItemRepository(session).get(loan.item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Loan's item not found")
    try:
        _circulation(session, actor=user).claim_returned(item.barcode)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Re-fetch the loan (still open; we surface the existing row)
    return LoanResponse.model_validate(SqlLoanRepository(session).get(loan_id))
