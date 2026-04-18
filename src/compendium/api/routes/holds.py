from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import CreateHoldRequest, HoldResponse
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.holds import HoldService

router = APIRouter()


def _holds(session: Session) -> HoldService:
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_expiry_days=get_settings().hold_expiry_days,
    )


@router.post("/", status_code=201, response_model=HoldResponse)
def place_hold(
    body: CreateHoldRequest,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("hold.place.self")),
) -> HoldResponse:
    try:
        hold = _holds(session).place(body.work_id, body.card_number)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return HoldResponse.model_validate(hold)


@router.get("/", response_model=list[HoldResponse])
def list_holds(
    card_number: str,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("hold.view.self")),
) -> list[HoldResponse]:
    patron = SqlPatronRepository(session).get_by_card_number(card_number)
    if patron is None:
        raise HTTPException(status_code=404, detail=f"No patron with card '{card_number}'")
    holds = SqlHoldRepository(session).get_active_for_patron(patron.id)
    return [HoldResponse.model_validate(h) for h in holds]


@router.delete("/{hold_id}", status_code=204)
def cancel_hold(
    hold_id: int = Path(),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("hold.place.self")),
) -> None:
    hold = SqlHoldRepository(session).get(hold_id)
    if hold is None:
        raise HTTPException(status_code=404, detail=f"No hold with id={hold_id}")
    try:
        _holds(session).cancel(hold_id, hold.patron_id)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
