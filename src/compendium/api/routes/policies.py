from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import CreatePolicyRequest, LoanPolicyResponse
from compendium.db.session import get_session
from compendium.domain.models import AppUser, LoanPolicy
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository

router = APIRouter()


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
    _user: AppUser = Depends(require_permission("policy.edit")),
) -> LoanPolicyResponse:
    repo = SqlLoanPolicyRepository(session)
    if body.is_default:
        existing = repo.get_default()
        if existing is not None:
            raise HTTPException(
                status_code=422,
                detail="A default policy already exists. Update it instead.",
            )
    policy = LoanPolicy(
        name=body.name,
        media_type_id=body.media_type_id,
        loan_period_days=body.loan_period_days,
        max_renewals=body.max_renewals,
        is_default=body.is_default,
    )
    repo.add(policy)
    return LoanPolicyResponse.model_validate(policy)
