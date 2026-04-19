from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import AuditLogResponse
from compendium.db.session import get_session
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.services.audit import AuditService

router = APIRouter()

_PERM = "patron.manage"


@router.get("/", response_model=list[AuditLogResponse])
def list_audit(
    entity_type: str = "",
    entity_id: str = "",
    user_id: str = "",
    limit: int = 50,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission(_PERM)),
) -> list[AuditLogResponse]:
    svc = AuditService(SqlAuditLogRepository(session))
    entity_id_int = int(entity_id) if entity_id.strip().isdigit() else None
    user_id_int = int(user_id) if user_id.strip().isdigit() else None
    entries = svc.list(
        entity_type=entity_type or None,
        entity_id=entity_id_int,
        user_id=user_id_int,
        limit=min(limit, 200),
    )
    return [AuditLogResponse.model_validate(e) for e in entries]
