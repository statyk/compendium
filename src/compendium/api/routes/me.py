"""Patron self-service endpoints — identity comes from the JWT, no card number needed."""

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from compendium.api.deps import get_current_patron, require_permission
from compendium.api.schemas import HoldResponse, LoanResponse, SelfHoldRequest
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser, Patron
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.circulation import CirculationService
from compendium.services.holds import HoldService

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


def _holds(session: Session) -> HoldService:
    settings = get_settings()
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
        hold_expiry_days=settings.hold_expiry_days,
        hold_pickup_days=settings.hold_pickup_days,
    )


@router.get("/loans", response_model=list[LoanResponse])
def my_loans(
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("loan.view.self")),
    patron: Patron = Depends(get_current_patron),
) -> list[LoanResponse]:
    loans = SqlLoanRepository(session).get_active_for_patron(patron.id)
    return [LoanResponse.model_validate(loan) for loan in loans]


@router.get("/holds", response_model=list[HoldResponse])
def my_holds(
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("hold.view.self")),
    patron: Patron = Depends(get_current_patron),
) -> list[HoldResponse]:
    holds = SqlHoldRepository(session).get_active_for_patron(patron.id)
    return [HoldResponse.model_validate(hold) for hold in holds]


@router.post("/holds", status_code=201, response_model=HoldResponse)
def place_hold(
    body: SelfHoldRequest,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("hold.place.self")),
    patron: Patron = Depends(get_current_patron),
) -> HoldResponse:
    try:
        hold = _holds(session).place(body.work_id, patron.library_card_number)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return HoldResponse.model_validate(hold)


@router.delete("/holds/{hold_id}", status_code=204)
def cancel_hold(
    hold_id: int = Path(),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("hold.place.self")),
    patron: Patron = Depends(get_current_patron),
) -> None:
    try:
        _holds(session).cancel(hold_id, patron.id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/loans/{loan_id}/renew", response_model=LoanResponse)
def renew_loan(
    loan_id: int = Path(),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("loan.renew.self")),
    patron: Patron = Depends(get_current_patron),
) -> LoanResponse:
    try:
        loan = _circulation(session).renew_by_id(loan_id, patron_id=patron.id)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LoanResponse.model_validate(loan)
