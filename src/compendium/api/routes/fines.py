"""API endpoints for fines and per-patron overdue materialization."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import (
    AssessManualFineRequest,
    AssessOverdueResponse,
    FineResponse,
    WaiveFineRequest,
)
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.fines import FineService

fines_router = APIRouter()
patron_fines_router = APIRouter()
me_fines_router = APIRouter()


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


# ── /patrons/{card}/fines and /patrons/{card}/fines/assess-overdue ────────────


@patron_fines_router.get("/{card_number}/fines", response_model=list[FineResponse])
def list_patron_fines(
    card_number: str,
    status: str | None = None,
    limit: int = 50,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("fine.manage")),
) -> list[FineResponse]:
    patron = SqlPatronRepository(session).get_by_card_number(card_number)
    if patron is None:
        raise HTTPException(status_code=404, detail=f"No patron '{card_number}'")
    fines = _fine_svc(session, user).list(patron_id=patron.id, status=status, limit=limit)
    return [FineResponse.model_validate(f) for f in fines]


@patron_fines_router.post(
    "/{card_number}/fines/assess-overdue", response_model=AssessOverdueResponse
)
def assess_overdue_for_patron(
    card_number: str,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("fine.manage")),
) -> AssessOverdueResponse:
    patron = SqlPatronRepository(session).get_by_card_number(card_number)
    if patron is None:
        raise HTTPException(status_code=404, detail=f"No patron '{card_number}'")
    counts = _fine_svc(session, user).assess_overdue_fines(patron_id=patron.id)
    return AssessOverdueResponse(**counts)


# ── /fines (manual assess + pay + waive) ─────────────────────────────────────


@fines_router.get("", response_model=list[FineResponse])
def list_outstanding_fines(
    kind: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("fine.manage")),
) -> list[FineResponse]:
    """System-wide list of outstanding fines."""
    fines = SqlFineRepository(session).list_outstanding(
        kind=kind, query=q, limit=limit, offset=offset
    )
    return [FineResponse.model_validate(f) for f in fines]


@fines_router.post("", response_model=FineResponse)
def assess_manual_fine(
    body: AssessManualFineRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("fine.manage")),
) -> FineResponse:
    patron = SqlPatronRepository(session).get_by_card_number(body.patron_card)
    if patron is None:
        raise HTTPException(status_code=404, detail=f"No patron '{body.patron_card}'")
    try:
        fine = _fine_svc(session, user).assess_manual(
            patron,
            kind=body.kind,
            amount_cents=body.amount_cents,
            note=body.note,
            reason=body.reason,
            loan_id=body.loan_id,
            item_id=body.item_id,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FineResponse.model_validate(fine)


@fines_router.post("/{fine_id}/pay", response_model=FineResponse)
def pay_fine(
    fine_id: int,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("fine.manage")),
) -> FineResponse:
    try:
        fine = _fine_svc(session, user).pay(fine_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FineResponse.model_validate(fine)


@fines_router.post("/{fine_id}/waive", response_model=FineResponse)
def waive_fine(
    fine_id: int,
    body: WaiveFineRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("fine.manage")),
) -> FineResponse:
    try:
        fine = _fine_svc(session, user).waive(fine_id, body.note)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FineResponse.model_validate(fine)


# ── /me/fines ─────────────────────────────────────────────────────────────────


@me_fines_router.get("/fines", response_model=list[FineResponse])
def list_my_fines(
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("fine.view.self")),
) -> list[FineResponse]:
    patron = SqlPatronRepository(session).get_by_user_id(user.id)
    if patron is None:
        return []
    fines = _fine_svc(session, user).list(patron_id=patron.id, limit=200)
    return [FineResponse.model_validate(f) for f in fines]

