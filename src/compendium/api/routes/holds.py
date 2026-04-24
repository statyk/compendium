from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import CreateHoldRequest, HoldResponse
from compendium.domain.errors import ValidationError
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.auth import has_permission
from compendium.services.holds import HoldService

router = APIRouter()


def _holds(session: Session, actor: AppUser | None = None) -> HoldService:
    settings = get_settings()
    return HoldService(
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
        item_repo=SqlItemRepository(session),
        hold_expiry_days=settings.hold_expiry_days,
        hold_pickup_days=settings.hold_pickup_days,
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
    )


def _require_self_or_any(user: AppUser, session: Session, card_number: str, any_perm: str) -> None:
    """Allow if caller holds the ``.any`` permission, or if the card belongs to them."""
    if has_permission(user.role.permissions, any_perm):
        return
    patron = SqlPatronRepository(session).get_by_user_id(user.id)
    if patron is None or patron.library_card_number != card_number:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/", status_code=201, response_model=HoldResponse)
def place_hold(
    body: CreateHoldRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("hold.place.self")),
) -> HoldResponse:
    _require_self_or_any(user, session, body.card_number, "hold.place.any")
    try:
        hold = _holds(session).place(body.work_id, body.card_number)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return HoldResponse.model_validate(hold)


@router.get("/", response_model=list[HoldResponse])
def list_holds(
    card_number: str | None = Query(default=None),
    status: str | None = Query(default=None),
    branch: str | None = Query(default=None, description="Branch code"),
    work_id: int | None = Query(default=None),
    q: str | None = Query(default=None, description="Search patron or work title"),
    older_than_days: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("hold.view.self")),
) -> list[HoldResponse]:
    """List holds.

    - With ``card_number``: returns that patron's active holds. Callers who
      don't hold ``hold.view.any`` may only query their own card.
    - Without ``card_number``: system-wide list with filters. Requires
      ``hold.view.any``.
    """
    if card_number is not None:
        _require_self_or_any(user, session, card_number, "hold.view.any")
        patron = SqlPatronRepository(session).get_by_card_number(card_number)
        if patron is None:
            raise HTTPException(
                status_code=404, detail=f"No patron with card '{card_number}'"
            )
        holds = SqlHoldRepository(session).get_active_for_patron(patron.id)
        return [HoldResponse.model_validate(h) for h in holds]

    if not has_permission(user.role.permissions, "hold.view.any"):
        raise HTTPException(status_code=403, detail="Forbidden")

    branch_id: int | None = None
    if branch:
        b = SqlBranchRepository(session).get_by_code(branch)
        if b is not None:
            branch_id = b.id
    holds = SqlHoldRepository(session).list_active(
        status=status,
        branch_id=branch_id,
        work_id=work_id,
        query=q,
        older_than_days=older_than_days,
        limit=limit,
        offset=offset,
    )
    return [HoldResponse.model_validate(h) for h in holds]


@router.get("/queue/{work_id}", response_model=list[HoldResponse])
def work_queue(
    work_id: int = Path(),
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("hold.view.any")),
) -> list[HoldResponse]:
    holds = SqlHoldRepository(session).queue_for_work(work_id)
    return [HoldResponse.model_validate(h) for h in holds]


@router.delete("/{hold_id}", status_code=204)
def cancel_hold(
    hold_id: int = Path(),
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("hold.place.self")),
) -> None:
    hold = SqlHoldRepository(session).get(hold_id)
    if hold is None:
        raise HTTPException(status_code=404, detail=f"No hold with id={hold_id}")
    if not has_permission(user.role.permissions, "hold.place.any"):
        caller_patron = SqlPatronRepository(session).get_by_user_id(user.id)
        if caller_patron is None or caller_patron.id != hold.patron_id:
            raise HTTPException(status_code=403, detail="Forbidden")
    try:
        _holds(session, actor=user).cancel(hold_id, hold.patron_id)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class SuspendHoldRequest(BaseModel):
    until: date
    reason: str | None = None


def _check_hold_owner_or_any(
    session: Session, user: AppUser, hold_id: int, any_perm: str
):
    hold = SqlHoldRepository(session).get(hold_id)
    if hold is None:
        raise HTTPException(status_code=404, detail=f"No hold with id={hold_id}")
    if not has_permission(user.role.permissions, any_perm):
        caller_patron = SqlPatronRepository(session).get_by_user_id(user.id)
        if caller_patron is None or caller_patron.id != hold.patron_id:
            raise HTTPException(status_code=403, detail="Forbidden")
    return hold


@router.post("/{hold_id}/suspend", response_model=HoldResponse)
def suspend_hold(
    hold_id: int,
    body: SuspendHoldRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("hold.place.self")),
) -> HoldResponse:
    _check_hold_owner_or_any(session, user, hold_id, "hold.place.any")
    try:
        hold = _holds(session, actor=user).suspend(
            hold_id, until=body.until, reason=body.reason
        )
    except (NotFoundError, BusinessRuleError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return HoldResponse.model_validate(hold)


@router.post("/{hold_id}/resume", response_model=HoldResponse)
def resume_hold(
    hold_id: int,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("hold.place.self")),
) -> HoldResponse:
    _check_hold_owner_or_any(session, user, hold_id, "hold.place.any")
    try:
        hold = _holds(session, actor=user).resume(hold_id)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return HoldResponse.model_validate(hold)
