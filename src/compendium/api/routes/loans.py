from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import CheckoutRequest, LoanResponse
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.circulation import CirculationService

router = APIRouter()


def _circulation(session: Session) -> CirculationService:
    settings = get_settings()
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        hold_pickup_days=settings.hold_pickup_days,
    )


@router.post("/checkout", status_code=201, response_model=LoanResponse)
def checkout(
    body: CheckoutRequest,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("loan.checkout")),
) -> LoanResponse:
    try:
        loan = _circulation(session).checkout(body.barcode, body.card_number)
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
    _user: AppUser = Depends(require_permission("loan.renew.self")),
) -> LoanResponse:
    try:
        loan = _circulation(session).renew_by_id(loan_id)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LoanResponse.model_validate(loan)
