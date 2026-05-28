"""REST API for library hours and closed-date calendar."""
from __future__ import annotations

from datetime import date, time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.calendar_repository import (
    SqlClosedDateRepository,
    SqlLibraryHoursRepository,
)
from compendium.services.audit import AuditService
from compendium.services.calendar import CalendarService
from compendium.services.site_settings import get_site_setting

router = APIRouter()

_PERM = "calendar.manage"


def _svc(session: Session, actor: AppUser) -> CalendarService:
    return CalendarService(
        hours_repo=SqlLibraryHoursRepository(session),
        closed_date_repo=SqlClosedDateRepository(session),
        timezone=get_site_setting("library_timezone"),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=actor.username,
        source="api",
    )


# ------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------

class LibraryHoursResponse(BaseModel):
    weekday: int
    is_open: bool
    open_time: time | None
    close_time: time | None

    model_config = {"from_attributes": True}


class UpdateLibraryHoursRequest(BaseModel):
    is_open: bool | None = None
    open_time: time | None = None
    close_time: time | None = None


class ClosedDateResponse(BaseModel):
    id: int
    start_date: date
    end_date: date
    label: str | None
    recurs_annually: bool

    model_config = {"from_attributes": True}


class CreateClosedDateRequest(BaseModel):
    start_date: date
    end_date: date | None = None
    label: str | None = None
    recurs_annually: bool = False


class UpdateClosedDateRequest(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    label: str | None = None
    recurs_annually: bool | None = None


# ------------------------------------------------------------------
# Library Hours endpoints
# ------------------------------------------------------------------

@router.get("/library-hours/", response_model=list[LibraryHoursResponse])
def list_hours(
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission(_PERM)),
):
    return [LibraryHoursResponse.model_validate(h)
            for h in SqlLibraryHoursRepository(session).list()]


@router.patch("/library-hours/{weekday}", response_model=LibraryHoursResponse)
def update_hours(
    weekday: int,
    payload: UpdateLibraryHoursRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission(_PERM)),
):
    try:
        row = _svc(session, user).update_weekday(
            weekday,
            is_open=payload.is_open,
            open_time=payload.open_time if payload.open_time is not None else ...,
            close_time=payload.close_time if payload.close_time is not None else ...,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LibraryHoursResponse.model_validate(row)


# ------------------------------------------------------------------
# Closed Dates endpoints
# ------------------------------------------------------------------

@router.get("/closed-dates/", response_model=list[ClosedDateResponse])
def list_closed_dates(
    limit: int = 100,
    offset: int = 0,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission(_PERM)),
):
    return [ClosedDateResponse.model_validate(cd)
            for cd in SqlClosedDateRepository(session).list(limit=limit, offset=offset)]


@router.post("/closed-dates/", response_model=ClosedDateResponse, status_code=201)
def create_closed_date(
    payload: CreateClosedDateRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission(_PERM)),
):
    try:
        cd = _svc(session, user).add_closed_date(
            payload.start_date,
            payload.end_date,
            label=payload.label,
            recurs_annually=payload.recurs_annually,
        )
    except (ValidationError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ClosedDateResponse.model_validate(cd)


@router.patch("/closed-dates/{closed_date_id}", response_model=ClosedDateResponse)
def update_closed_date(
    closed_date_id: int,
    payload: UpdateClosedDateRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission(_PERM)),
):
    try:
        cd = _svc(session, user).update_closed_date(
            closed_date_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
            label=payload.label if payload.label is not None else ...,
            recurs_annually=payload.recurs_annually,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValidationError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ClosedDateResponse.model_validate(cd)


@router.delete("/closed-dates/{closed_date_id}", status_code=204)
def delete_closed_date(
    closed_date_id: int,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission(_PERM)),
):
    try:
        _svc(session, user).delete_closed_date(closed_date_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
