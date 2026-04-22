"""API endpoints for notification admin log + manual retry."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import NotificationResponse
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import NotFoundError, ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.notification_repository import SqlNotificationRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.notifications import NotificationService

router = APIRouter()


def _svc(session: Session, user: AppUser | None) -> NotificationService:
    return NotificationService(
        notification_repo=SqlNotificationRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
        patron_repo=SqlPatronRepository(session),
        settings=get_settings(),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=user,
        source="api",
    )


@router.get("", response_model=list[NotificationResponse])
def list_notifications(
    status: str | None = Query(default=None),
    template_key: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("notification.manage")),
) -> list[NotificationResponse]:
    rows = _svc(session, user).list(
        status=status, template_key=template_key, limit=limit, offset=offset
    )
    return [NotificationResponse.model_validate(r) for r in rows]


@router.post("/{notification_id}/retry", response_model=NotificationResponse)
def retry_notification(
    notification_id: int,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("notification.manage")),
) -> NotificationResponse:
    try:
        row = _svc(session, user).retry(notification_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return NotificationResponse.model_validate(row)
