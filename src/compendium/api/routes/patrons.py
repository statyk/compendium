from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import (
    CreatePatronRequest,
    PatronResponse,
    UpdatePatronRequest,
)
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_category_repository import (
    SqlPatronCategoryRepository,
)
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.patrons import PatronService, _MISSING

router = APIRouter()


def _resolve_category_id(session: Session, code: str | None) -> int | None:
    if code is None:
        return None
    cat = SqlPatronCategoryRepository(session).get_by_code(code.lower())
    if cat is None:
        raise HTTPException(
            status_code=422, detail=f"No patron category with code '{code}'"
        )
    return cat.id


def _patron_service(session: Session, actor: AppUser, source: str = "api") -> PatronService:
    return PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source=source,
    )


@router.post("", status_code=201, response_model=PatronResponse)
def create_patron(
    body: CreatePatronRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("patron.manage")),
) -> PatronResponse:
    category_id = _resolve_category_id(session, body.category_code)
    patron = _patron_service(session, user).create(
        full_name=body.full_name,
        contact_email=body.contact_email,
        contact_phone=body.contact_phone,
        category_id=category_id,
        expires_at=body.expires_at,
    )
    return PatronResponse.model_validate(patron)


@router.patch("/{card_number}", response_model=PatronResponse)
def update_patron(
    card_number: str,
    body: UpdatePatronRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("patron.manage")),
) -> PatronResponse:
    fields = body.model_fields_set
    cat_arg: object = _MISSING
    if "category_code" in fields:
        cat_arg = (
            _resolve_category_id(session, body.category_code)
            if body.category_code is not None
            else None
        )
    exp_arg: object = _MISSING
    if "expires_at" in fields:
        exp_arg = body.expires_at
    if cat_arg is _MISSING and exp_arg is _MISSING:
        raise HTTPException(status_code=422, detail="No fields to update")
    try:
        patron = _patron_service(session, user).update(
            card_number, category_id=cat_arg, expires_at=exp_arg
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (BusinessRuleError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PatronResponse.model_validate(patron)


@router.post("/{card_number}/deactivate", response_model=PatronResponse)
def deactivate_patron(
    card_number: str,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("patron.manage")),
) -> PatronResponse:
    try:
        patron = _patron_service(session, user).deactivate(card_number)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PatronResponse.model_validate(patron)
