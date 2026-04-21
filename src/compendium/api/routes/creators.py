from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import CreatorRename, CreatorSummary
from compendium.db.session import get_session
from compendium.domain.errors import (
    BusinessRuleError,
    NotFoundError,
    ValidationError,
)
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService

router = APIRouter()


def _catalog(session: Session, actor: AppUser | None = None) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
    )


@router.patch("/{creator_id}", response_model=CreatorSummary)
def rename_creator(
    creator_id: int,
    payload: CreatorRename,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("work.edit")),
) -> CreatorSummary:
    try:
        creator = _catalog(session, user).update_creator(
            creator_id, display_name=payload.display_name
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (BusinessRuleError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return CreatorSummary.model_validate(creator)
