from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from compendium.api.deps import get_optional_user, require_permission
from compendium.services.auth import has_permission
from compendium.api.schemas import (
    WorkCreatorsReplace,
    WorkDetail,
    WorkSummary,
    WorkUpdate,
)
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.services.site_settings import get_site_setting
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
from compendium.services.discovery import DiscoveryService

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


def _discovery(session: Session) -> DiscoveryService:
    return DiscoveryService(work_repo=SqlWorkRepository(session))


def _gate_search(user: AppUser | None) -> None:
    if not get_site_setting("guest_search_enabled") and user is None:
        raise HTTPException(status_code=401, detail="Authentication required to search")


@router.get("/search", response_model=list[WorkSummary])
def search_works(
    q: str = "",
    field: str = "all",
    media: str = "",
    decade: int | None = None,
    available_only: bool = False,
    include_withdrawn: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    session: Session = Depends(get_session),
    user: AppUser | None = Depends(get_optional_user),
) -> list[WorkSummary]:
    _gate_search(user)
    media_codes = [c.strip() for c in media.split(",") if c.strip()]
    can_include = user is not None and has_permission(user.role.permissions, "item.edit")
    page_obj = _discovery(session).search(
        q,
        field=field,
        page=page,
        page_size=page_size,
        media_type_codes=media_codes,
        decade=decade,
        available_only=available_only,
        include_withdrawn_only=include_withdrawn and can_include,
    )
    return [WorkSummary.model_validate(w) for w in page_obj.works]


@router.get("/new-arrivals", response_model=list[WorkSummary])
def new_arrivals(
    days: int = Query(60, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
    include_withdrawn: bool = False,
    session: Session = Depends(get_session),
    user: AppUser | None = Depends(get_optional_user),
) -> list[WorkSummary]:
    _gate_search(user)
    can_include = user is not None and has_permission(user.role.permissions, "item.edit")
    works = _discovery(session).new_arrivals(
        days=days, limit=limit, include_withdrawn_only=include_withdrawn and can_include
    )
    return [WorkSummary.model_validate(w) for w in works]


@router.get("/recently-returned", response_model=list[WorkSummary])
def recently_returned(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
    include_withdrawn: bool = False,
    session: Session = Depends(get_session),
    user: AppUser | None = Depends(get_optional_user),
) -> list[WorkSummary]:
    _gate_search(user)
    can_include = user is not None and has_permission(user.role.permissions, "item.edit")
    works = _discovery(session).recently_returned(
        days=days, limit=limit, include_withdrawn_only=include_withdrawn and can_include
    )
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


def _serialize_refresh_report(report) -> dict:
    return {
        "work_id": report.work_id,
        "source": report.source,
        "lookup_kind": report.lookup_kind,
        "lookup_value": report.lookup_value,
        "found": report.found,
        "error": report.error,
        "applied": report.applied,
        "cover_cache_busted": report.cover_cache_busted,
        # Convert tuple values to JSON-friendly dicts.
        "planned": {
            k: {"current": old, "new": new}
            for k, (old, new) in report.planned.items()
        },
    }


@router.get("/{work_id}/refresh-metadata")
def preview_refresh_metadata(
    work_id: int,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("work.edit")),
) -> dict:
    """Dry-run a metadata refresh — returns the planned diff without committing."""
    try:
        report = _catalog(session, user).refresh_metadata(work_id, dry_run=True)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_refresh_report(report)


@router.post("/{work_id}/refresh-metadata")
def apply_refresh_metadata(
    work_id: int,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("work.edit")),
) -> dict:
    """Apply a metadata refresh: commits fill-missing diff + busts cover cache."""
    try:
        report = _catalog(session, user).refresh_metadata(work_id, dry_run=False)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_refresh_report(report)


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
