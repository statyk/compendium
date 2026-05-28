from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import (
    AddHouseholdMemberRequest,
    CreateHouseholdRequest,
    HouseholdListResponse,
    HouseholdResponse,
    PatronResponse,
    UpdateHouseholdRequest,
)
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.household_repository import SqlHouseholdRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.households import HouseholdService, _MISSING

router = APIRouter()

_PERM = "household.manage"


def _svc(session: Session, actor: AppUser) -> HouseholdService:
    return HouseholdService(
        household_repo=SqlHouseholdRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
    )


@router.post("", status_code=201, response_model=HouseholdResponse)
def create_household(
    body: CreateHouseholdRequest,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    try:
        return _svc(session, actor).create(name=body.name, notes=body.notes)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("", response_model=list[HouseholdListResponse])
def list_households(
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    svc = _svc(session, actor)
    households = svc.list(limit=limit, offset=offset)
    patron_repo = SqlPatronRepository(session)
    return [
        {
            "id": hh.id,
            "name": hh.name,
            "notes": hh.notes,
            "member_count": len(patron_repo.list_by_household(hh.id)),
        }
        for hh in households
    ]


@router.get("/{household_id}", response_model=HouseholdResponse)
def get_household(
    household_id: int,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    try:
        return _svc(session, actor).get(household_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{household_id}", response_model=HouseholdResponse)
def update_household(
    household_id: int,
    body: UpdateHouseholdRequest,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    try:
        return _svc(session, actor).update(
            household_id,
            name=body.name if body.name is not None else _MISSING,
            notes=body.notes if body.notes is not None else _MISSING,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.delete("/{household_id}", status_code=204)
def delete_household(
    household_id: int,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    try:
        _svc(session, actor).delete(household_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BusinessRuleError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{household_id}/members", response_model=PatronResponse)
def add_member(
    household_id: int,
    body: AddHouseholdMemberRequest,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    try:
        return _svc(session, actor).add_member(household_id, body.card_number)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BusinessRuleError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.delete("/{household_id}/members/{card_number}", response_model=PatronResponse)
def remove_member(
    household_id: int,
    card_number: str,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    try:
        return _svc(session, actor).remove_member(household_id, card_number)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BusinessRuleError as e:
        raise HTTPException(status_code=422, detail=str(e))
