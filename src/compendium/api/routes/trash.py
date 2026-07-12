from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import DeletedWorkSummaryOut, WorkDetail
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.trash_repository import SqlTrashRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.trash import TrashService

router = APIRouter()


def _svc(session: Session, actor: AppUser) -> TrashService:
    return TrashService(
        trash_repo=SqlTrashRepository(session),
        work_repo=SqlWorkRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
    )


@router.get("", response_model=list[DeletedWorkSummaryOut])
def list_trash(
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("work.delete")),
) -> list[DeletedWorkSummaryOut]:
    rows = _svc(session, user).list_deleted_works(limit=limit)
    return [DeletedWorkSummaryOut.model_validate(r) for r in rows]


@router.post("/{trash_id}/restore", response_model=WorkDetail)
def restore_work(
    trash_id: int,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("work.delete")),
) -> WorkDetail:
    try:
        work = _svc(session, user).restore_work(trash_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return WorkDetail.model_validate(work)


@router.delete("/{trash_id}")
def purge_trash_entry(
    trash_id: int,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("work.delete")),
) -> dict:
    try:
        purged = _svc(session, user).purge(trash_id=trash_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"purged": purged}
