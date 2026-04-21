from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from compendium.api.deps import get_optional_user, require_permission
from compendium.api.schemas import (
    WorkCreatorsReplace,
    WorkDetail,
    WorkSummary,
    WorkUpdate,
)
from compendium.db.engine import get_settings
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


@router.get("/search", response_model=list[WorkSummary])
def search_works(
    q: str = Query(min_length=1),
    session: Session = Depends(get_session),
    user: AppUser | None = Depends(get_optional_user),
) -> list[WorkSummary]:
    settings = get_settings()
    if not settings.guest_search_enabled and user is None:
        raise HTTPException(status_code=401, detail="Authentication required to search")
    works = SqlWorkRepository(session).search(q)
    return [WorkSummary.model_validate(w) for w in works]


@router.patch("/{work_id}", response_model=WorkDetail)
def update_work(
    work_id: int,
    payload: WorkUpdate,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("work.edit")),
) -> WorkDetail:
    kwargs = payload.model_dump(include=payload.model_fields_set)
    try:
        work = _catalog(session, user).update_work(work_id, **kwargs)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (BusinessRuleError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return WorkDetail.model_validate(work)


@router.put("/{work_id}/creators", response_model=WorkDetail)
def replace_work_creators(
    work_id: int,
    payload: WorkCreatorsReplace,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("work.edit")),
) -> WorkDetail:
    pairs = [(c.name, c.role) for c in payload.creators]
    try:
        work = _catalog(session, user).replace_creators(work_id, pairs)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (BusinessRuleError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return WorkDetail.model_validate(work)
