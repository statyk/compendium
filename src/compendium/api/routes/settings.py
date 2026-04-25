"""REST endpoints for site_setting overrides.

Read access is gated per-scope:
- Librarian-tier descriptors require ``patron.manage`` (Librarian preset
  covers).
- System-tier descriptors require ``system.manage``.

Writes (PATCH) require the matching scope's permission. A patch may include
both scopes; per-key gates run row-by-row, so a librarian can only flip
librarian-tier keys.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.db.session import get_session
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.services.audit import AuditService
from compendium.services.auth import has_permission
from compendium.services.settings_registry import (
    SettingValidationError,
    UnknownSettingError,
    all_descriptors,
    get_descriptor,
)
from compendium.services.site_settings import (
    delete_site_setting,
    get_site_setting,
    set_site_setting,
)

router = APIRouter()


class SettingResponse(BaseModel):
    key: str
    scope: str
    type: str  # rendered as a human-readable hint, e.g. "str", "bool", "list[int]"
    nullable: bool
    default: Any
    value: Any
    env_var: str
    env_overridden: bool
    help_text: str


class SettingPatch(BaseModel):
    value: Any | None = None  # None means "reset to default"


def _scope_perm(scope: str) -> str:
    return "system.manage" if scope == "system" else "patron.manage"


def _serialize(desc, *, value: Any) -> SettingResponse:
    type_repr = (
        "list[int]"
        if str(desc.type).startswith("list[int]")
        else (
            "list[str]"
            if str(desc.type).startswith("list[str]")
            else (str(desc.type).removeprefix("typing.").removeprefix("<class '").removesuffix("'>"))
        )
    )
    return SettingResponse(
        key=desc.key,
        scope=desc.scope,
        type=type_repr,
        nullable=desc.nullable,
        default=desc.default,
        value=value,
        env_var=desc.resolved_env_var(),
        env_overridden=os.environ.get(desc.resolved_env_var()) is not None,
        help_text=desc.help_text,
    )


@router.get("/", response_model=list[SettingResponse])
def list_settings(
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("patron.manage")),
) -> list[SettingResponse]:
    """List every setting the caller is authorized to see (librarian + any
    system perms they hold)."""
    out: list[SettingResponse] = []
    can_system = has_permission(user.role.permissions, "system.manage")
    for desc in all_descriptors():
        if desc.scope == "system" and not can_system:
            continue
        try:
            value = get_site_setting(desc.key)
        except SettingValidationError:
            value = desc.default
        out.append(_serialize(desc, value=value))
    return sorted(out, key=lambda s: (s.scope, s.key))


@router.get("/{key}", response_model=SettingResponse)
def get_setting(
    key: str,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("patron.manage")),
) -> SettingResponse:
    try:
        desc = get_descriptor(key)
    except UnknownSettingError:
        raise HTTPException(status_code=404, detail=f"unknown setting: {key}")
    if desc.scope == "system" and not has_permission(
        user.role.permissions, "system.manage"
    ):
        raise HTTPException(status_code=403, detail="system.manage required")
    try:
        value = get_site_setting(key)
    except SettingValidationError:
        value = desc.default
    return _serialize(desc, value=value)


@router.patch("/{key}", response_model=SettingResponse)
def patch_setting(
    key: str,
    body: SettingPatch,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("patron.manage")),
) -> SettingResponse:
    """Set a single setting. ``value=null`` resets to default (deletes row)."""
    try:
        desc = get_descriptor(key)
    except UnknownSettingError:
        raise HTTPException(status_code=404, detail=f"unknown setting: {key}")
    if desc.scope == "system" and not has_permission(
        user.role.permissions, "system.manage"
    ):
        raise HTTPException(status_code=403, detail="system.manage required")

    audit_svc = AuditService(SqlAuditLogRepository(session))
    if body.value is None and not desc.nullable:
        # Treat null on a non-nullable field as "reset to default" — the API
        # contract is that null means "no override", so deleting the row is
        # the closest faithful interpretation.
        delete_site_setting(
            key, session=session, audit_svc=audit_svc, actor=user, source="api"
        )
        return _serialize(desc, value=desc.default)

    try:
        set_site_setting(
            key,
            body.value,
            session=session,
            updated_by_id=user.id,
            audit_svc=audit_svc,
            actor=user,
            source="api",
        )
    except SettingValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Echo the value the caller just set rather than re-reading the DB —
    # the dependency-injection wrapper commits after the response is built,
    # so a fresh-session read here would still see the pre-write state.
    return _serialize(desc, value=body.value)


@router.delete("/{key}", response_model=SettingResponse)
def reset_setting(
    key: str,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("patron.manage")),
) -> SettingResponse:
    """Delete the override row, reverting to the registered default."""
    try:
        desc = get_descriptor(key)
    except UnknownSettingError:
        raise HTTPException(status_code=404, detail=f"unknown setting: {key}")
    if desc.scope == "system" and not has_permission(
        user.role.permissions, "system.manage"
    ):
        raise HTTPException(status_code=403, detail="system.manage required")
    audit_svc = AuditService(SqlAuditLogRepository(session))
    delete_site_setting(
        key, session=session, audit_svc=audit_svc, actor=user, source="api"
    )
    return _serialize(desc, value=desc.default)
