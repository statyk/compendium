from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import (
    CreateUserRequest,
    PatronLinkRequest,
    PatronResponse,
    UserResponse,
)
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.audit import AuditService
from compendium.services.auth import AuthService, assignable_roles
from compendium.services.patrons import PatronService

router = APIRouter()


def _auth(session: Session, actor: AppUser) -> AuthService:
    return AuthService(
        user_repo=SqlUserRepository(session),
        role_repo=SqlRoleRepository(session),
        settings=get_settings(),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
    )


def _patron_svc(session: Session, actor: AppUser) -> PatronService:
    return PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
    )


@router.post("", status_code=201, response_model=UserResponse)
def create_user(
    body: CreateUserRequest,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission("user.manage")),
) -> UserResponse:
    all_roles = SqlRoleRepository(session).list()
    allowed_names = {r.name for r in assignable_roles(actor.role.permissions, all_roles)}
    if body.role_name not in allowed_names:
        raise HTTPException(status_code=403, detail=f"You cannot assign the '{body.role_name}' role")
    if body.role_name == "Patron" and body.patron is None:
        raise HTTPException(status_code=422, detail="Patron-role users must be linked to a patron record")
    if body.role_name != "Patron" and body.patron is not None:
        raise HTTPException(status_code=422, detail="Patron block is only valid when role is 'Patron'")
    try:
        new_user = _auth(session, actor).create_user(
            body.username, body.password, body.role_name, email=body.email
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (BusinessRuleError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.patron is not None:
        psvc = _patron_svc(session, actor)
        try:
            if body.patron.link_card is not None:
                psvc.link_user(body.patron.link_card, new_user.id)
            elif body.patron.create is not None:
                c = body.patron.create
                psvc.create(
                    full_name=c.full_name,
                    contact_email=c.contact_email,
                    contact_phone=c.contact_phone,
                    user_id=new_user.id,
                    expires_at=c.expires_at,
                )
            else:
                raise HTTPException(
                    status_code=422,
                    detail="Patron block must specify 'link_card' or 'create'",
                )
        except (NotFoundError, BusinessRuleError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return UserResponse.model_validate(new_user)


@router.post("/{username}/deactivate", response_model=UserResponse)
def deactivate_user(
    username: str,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("user.manage")),
) -> UserResponse:
    if username == user.username:
        raise HTTPException(status_code=422, detail="You cannot deactivate your own account")
    try:
        result = _auth(session, user).deactivate_user(username)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return UserResponse.model_validate(result)


@router.post("/{username}/reactivate", response_model=UserResponse)
def reactivate_user(
    username: str,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("user.manage")),
) -> UserResponse:
    try:
        result = _auth(session, user).reactivate_user(username)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return UserResponse.model_validate(result)


@router.post("/{username}/patron", response_model=PatronResponse)
def link_patron(
    username: str,
    body: PatronLinkRequest,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission("user.manage")),
) -> PatronResponse:
    target = SqlUserRepository(session).get_by_username(username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No user with username '{username}'")
    try:
        patron = _patron_svc(session, actor).link_user(body.card_number, target.id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PatronResponse.model_validate(patron)


@router.delete("/{username}/patron", response_model=PatronResponse)
def unlink_patron(
    username: str,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission("user.manage")),
) -> PatronResponse:
    target = SqlUserRepository(session).get_by_username(username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No user with username '{username}'")
    patron = SqlPatronRepository(session).get_by_user_id(target.id)
    if patron is None:
        raise HTTPException(status_code=404, detail=f"User '{username}' has no linked patron record")
    try:
        result = _patron_svc(session, actor).unlink_user(patron.library_card_number)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PatronResponse.model_validate(result)
