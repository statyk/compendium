"""REST endpoints for circulation & overdue reports."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import (
    DormantItemResponse,
    MonthlyCheckoutsResponse,
    OverdueLoanResponse,
    PopularWorkResponse,
)
from compendium.db.session import get_session
from compendium.domain.models import AppUser
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.reports import ReportsService

router = APIRouter()

_PERM = "report.view"


def _svc(session: Session) -> ReportsService:
    return ReportsService(
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
    )


def _parse_date(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD") from exc


@router.get("/checkouts", response_model=list[MonthlyCheckoutsResponse])
def checkouts(
    months: int = Query(12, ge=1, le=60),
    branch: str | None = None,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission(_PERM)),
):
    rows = _svc(session).checkouts_per_month(months=months, branch_code=branch)
    return [MonthlyCheckoutsResponse(month=r.month, count=r.count) for r in rows]


@router.get("/popular", response_model=list[PopularWorkResponse])
def popular(
    since: str = Query(..., alias="from"),
    until: str | None = Query(None, alias="to"),
    limit: int = Query(20, ge=1, le=200),
    branch: str | None = None,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission(_PERM)),
):
    since_dt = _parse_date(since)
    until_dt = _parse_date(until) if until else None
    rows = _svc(session).popular_works(
        since=since_dt, until=until_dt, limit=limit, branch_code=branch
    )
    return [
        PopularWorkResponse(
            work_id=r.work_id,
            title=r.title,
            subtitle=r.subtitle,
            media_type_code=r.media_type_code,
            checkout_count=r.checkout_count,
        )
        for r in rows
    ]


@router.get("/dormant", response_model=list[DormantItemResponse])
def dormant(
    not_since: str = Query(..., alias="not_since"),
    limit: int = Query(100, ge=1, le=500),
    branch: str | None = None,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission(_PERM)),
):
    cutoff = _parse_date(not_since)
    rows = _svc(session).dormant_items(
        not_since=cutoff, limit=limit, branch_code=branch
    )
    return [
        DormantItemResponse(
            item_id=r.item_id,
            barcode=r.barcode,
            title=r.title,
            media_type_code=r.media_type_code,
            branch_code=r.branch_code,
            last_checkout_at=r.last_checkout_at,
        )
        for r in rows
    ]


@router.get("/overdues", response_model=list[OverdueLoanResponse])
def overdues(
    branch: str | None = None,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission(_PERM)),
):
    rows = _svc(session).current_overdues(branch_code=branch)
    return [
        OverdueLoanResponse(
            loan_id=r.loan_id,
            patron_card=r.patron_card,
            patron_name=r.patron_name,
            item_barcode=r.item_barcode,
            title=r.title,
            due_at=r.due_at,
            days_overdue=r.days_overdue,
        )
        for r in rows
    ]
