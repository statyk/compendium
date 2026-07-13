from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import CreatePolicyRequest, LoanPolicyResponse
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.services.audit import AuditService
from compendium.services.policies import PolicyService

router = APIRouter()


def _policy_service(session: Session, actor: AppUser) -> PolicyService:
    return PolicyService(
        policy_repo=SqlLoanPolicyRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
    )


@router.get("/", response_model=list[LoanPolicyResponse])
def list_policies(
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("item.view")),
) -> list[LoanPolicyResponse]:
    policies = SqlLoanPolicyRepository(session).list()
    return [LoanPolicyResponse.model_validate(p) for p in policies]


@router.post("/", status_code=201, response_model=LoanPolicyResponse)
def create_policy(
    body: CreatePolicyRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("policy.edit")),
) -> LoanPolicyResponse:
    try:
        policy = _policy_service(session, user).create(
            name=body.name,
            loan_period_days=body.loan_period_days,
            max_renewals=body.max_renewals,
            media_type_id=body.media_type_id,
            patron_category_id=body.patron_category_id,
            is_default=body.is_default,
        )
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LoanPolicyResponse.model_validate(policy)


@router.delete("/{policy_id}", status_code=204)
def delete_policy(
    policy_id: int,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("policy.edit")),
) -> None:
    try:
        _policy_service(session, user).delete(policy_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
