"""Patron category REST endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import (
    CreatePatronCategoryRequest,
    PatronCategoryResponse,
    UpdatePatronCategoryRequest,
)
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.patron_category_repository import (
    SqlPatronCategoryRepository,
)
from compendium.services.audit import AuditService
from compendium.services.patron_categories import PatronCategoryService

router = APIRouter()

_PERM = "patron.manage"


def _svc(session: Session, actor: AppUser) -> PatronCategoryService:
    return PatronCategoryService(
        repo=SqlPatronCategoryRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
    )


@router.get("/", response_model=list[PatronCategoryResponse])
def list_categories(
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission(_PERM)),
):
    cats = SqlPatronCategoryRepository(session).list()
    return [PatronCategoryResponse.model_validate(c) for c in cats]


@router.post("/", response_model=PatronCategoryResponse, status_code=201)
def create_category(
    payload: CreatePatronCategoryRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission(_PERM)),
):
    try:
        cat = _svc(session, user).create(
            payload.code, payload.display_name, is_default=payload.is_default
        )
    except (BusinessRuleError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PatronCategoryResponse.model_validate(cat)


@router.patch("/{category_id}", response_model=PatronCategoryResponse)
def update_category(
    category_id: int,
    payload: UpdatePatronCategoryRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission(_PERM)),
):
    try:
        cat = _svc(session, user).update(
            category_id,
            display_name=payload.display_name,
            is_default=payload.is_default,
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (BusinessRuleError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PatronCategoryResponse.model_validate(cat)


@router.delete("/{category_id}", status_code=204)
def delete_category(
    category_id: int,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission(_PERM)),
):
    try:
        _svc(session, user).delete(category_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
