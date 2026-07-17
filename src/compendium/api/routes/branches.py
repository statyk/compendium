from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import BranchResponse, UpdateBranchRequest
from compendium.db.session import get_session
from compendium.domain.models import AppUser
from compendium.repositories.sql.branch_repository import SqlBranchRepository

router = APIRouter()

_VALID_SCHEMES = {"lcc", "ddc", "none"}


@router.get("/", response_model=list[BranchResponse])
def list_branches(
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("branch.edit")),
) -> list[BranchResponse]:
    branches = SqlBranchRepository(session).list()
    return [BranchResponse.model_validate(b) for b in branches]


@router.patch("/{branch_id}", response_model=BranchResponse)
def update_branch(
    branch_id: int,
    body: UpdateBranchRequest,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("branch.edit")),
) -> BranchResponse:
    repo = SqlBranchRepository(session)
    branch = repo.get(branch_id)
    if branch is None:
        raise HTTPException(status_code=404, detail="Branch not found")
    if body.name is not None:
        stripped = body.name.strip()
        if not stripped or len(stripped) > 128:
            raise HTTPException(status_code=422, detail="Invalid branch name")
        branch.name = stripped
    if body.default_classification_scheme is not None:
        scheme = body.default_classification_scheme.lower()
        if scheme not in _VALID_SCHEMES:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid classification scheme '{scheme}'. Must be one of: lcc, ddc, none.",
            )
        branch.default_classification_scheme = scheme
    repo.update(branch)
    return BranchResponse.model_validate(branch)
