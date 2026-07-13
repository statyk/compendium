from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import (
    CreateAccountInline,
    CreatePatronRequest,
    PatronResponse,
    UpdatePatronRequest,
)
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_category_repository import (
    SqlPatronCategoryRepository,
)
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.audit import AuditService
from compendium.services.auth import AuthService, has_permission
from compendium.services.patrons import PatronService

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


def _patron_service_with_auth(session: Session, actor: AppUser, source: str = "api") -> PatronService:
    auth_svc = AuthService(
        user_repo=SqlUserRepository(session),
        role_repo=SqlRoleRepository(session),
        settings=get_settings(),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source=source,
    )
    return PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source=source,
        auth_svc=auth_svc,
    )


@router.get("", response_model=list[PatronResponse])
def list_patrons(
    q: str | None = Query(default=None),
    status: str = Query(default="active", pattern="^(active|inactive|all)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: AppUser = Depends(require_permission("patron.manage")),
    session: Session = Depends(get_session),
):
    return SqlPatronRepository(session).list(
        limit=limit,
        offset=offset,
        status=status,
        query=(q or "").strip() or None,
    )


@router.post("", status_code=201, response_model=PatronResponse)
def create_patron(
    body: CreatePatronRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("patron.manage")),
) -> PatronResponse:
    category_id = _resolve_category_id(session, body.category_code)
    if body.account is not None:
        if not has_permission(user.role.permissions, "patron.account.manage"):
            raise HTTPException(status_code=403, detail="Forbidden: patron.account.manage required")
        try:
            patron = _patron_service_with_auth(session, user).create_with_account(
                full_name=body.full_name,
                contact_email=body.contact_email,
                contact_phone=body.contact_phone,
                category_id=category_id,
                expires_at=body.expires_at,
                username=body.account.username,
                password=body.account.password,
            )
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (BusinessRuleError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        patron = _patron_service(session, user).create(
            full_name=body.full_name,
            contact_email=body.contact_email,
            contact_phone=body.contact_phone,
            category_id=category_id,
            expires_at=body.expires_at,
        )
    return PatronResponse.model_validate(patron)


@router.post("/{card_number}/account", response_model=PatronResponse)
def create_patron_account(
    card_number: str,
    body: CreateAccountInline,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("patron.account.manage")),
) -> PatronResponse:
    try:
        patron = _patron_service_with_auth(session, user).create_account_for_patron(
            card_number, username=body.username, password=body.password
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (BusinessRuleError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PatronResponse.model_validate(patron)


@router.patch("/{card_number}", response_model=PatronResponse)
def update_patron(
    card_number: str,
    body: UpdatePatronRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("patron.manage")),
) -> PatronResponse:
    fields = body.model_fields_set
    update_kwargs: dict[str, object] = {}
    if "category_code" in fields:
        update_kwargs["category_id"] = (
            _resolve_category_id(session, body.category_code)
            if body.category_code is not None
            else None
        )
    if "expires_at" in fields:
        update_kwargs["expires_at"] = body.expires_at
    if "full_name" in fields:
        if body.full_name is None:
            raise HTTPException(status_code=422, detail="full_name cannot be null")
        update_kwargs["full_name"] = body.full_name
    if "contact_email" in fields:
        update_kwargs["contact_email"] = body.contact_email
    if "contact_phone" in fields:
        update_kwargs["contact_phone"] = body.contact_phone
    if not update_kwargs:
        raise HTTPException(status_code=422, detail="No fields to update")
    try:
        patron = _patron_service(session, user).update(card_number, **update_kwargs)
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


@router.post("/{card_number}/reactivate", response_model=PatronResponse)
def reactivate_patron(
    card_number: str,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("patron.manage")),
) -> PatronResponse:
    try:
        patron = _patron_service(session, user).reactivate(card_number)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PatronResponse.model_validate(patron)
