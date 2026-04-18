from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.services.audit import AuditEntityType, AuditService
from compendium.web.csrf import ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "patron.manage"

_ENTITY_CHOICES = [
    ("", "All types"),
    (AuditEntityType.WORK, "Work"),
    (AuditEntityType.ITEM, "Item"),
    (AuditEntityType.PATRON, "Patron"),
    (AuditEntityType.USER, "User"),
    (AuditEntityType.POLICY, "Policy"),
]


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean, status_code=status_code)
    if fresh:
        set_csrf_cookie(resp, fresh, get_settings().jwt_secret_key)
    return resp


@router.get("/audit")
def audit_list(
    request: Request,
    entity_type: str = "",
    entity_id: str = "",
    limit: int = 50,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    svc = AuditService(SqlAuditLogRepository(session))
    entity_id_int = int(entity_id) if entity_id.strip().isdigit() else None
    entries = svc.list(
        entity_type=entity_type or None,
        entity_id=entity_id_int,
        limit=min(limit, 200),
    )
    return _render(
        "audit/list.html",
        request,
        {
            "request": request,
            "user": user,
            "entries": entries,
            "entity_choices": _ENTITY_CHOICES,
            "selected_type": entity_type,
            "selected_id": entity_id,
            "limit": limit,
        },
    )
