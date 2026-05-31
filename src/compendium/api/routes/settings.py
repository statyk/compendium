"""REST endpoints for site_setting overrides.

Read access is gated per-scope:
- Librarian-tier descriptors require ``patron.manage`` (Librarian preset
  covers).
- System-tier descriptors require ``system.manage``.

Writes (PATCH) require the matching scope's permission. A patch may include
both scopes; per-key gates run row-by-row, so a librarian can only flip
librarian-tier keys.

Secret settings (``secret=True`` in the registry) are **write-only** from
the API: GET and list responses return ``value=null, is_set=<bool>`` rather
than the decrypted plaintext.  This mirrors the web UI's masking behaviour
and enforces the "secrets are never echoed" invariant.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.db.session import get_session
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.site_setting_repository import SqlSiteSettingRepository
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
    # For secret settings value is always null; is_set indicates whether a
    # non-empty value is configured (either via env var or a DB row).
    is_set: bool = False
    env_var: str
    env_overridden: bool
    help_text: str


class SettingPatch(BaseModel):
    value: Any | None = None  # None means "reset to default"
    force_skip_validation: bool = False  # Skip pre-save validators (e.g. GB key live check)


def _scope_perm(scope: str) -> str:
    return "system.manage" if scope == "system" else "patron.manage"


def _secret_is_set(key: str, session: Session) -> bool:
    """Return True if a non-empty value for *key* is configured (env or DB).

    Deliberately does NOT decrypt — used to populate ``is_set`` without
    echoing the plaintext secret.
    """
    from compendium.services.settings_registry import get_descriptor as _gd
    try:
        desc = _gd(key)
    except UnknownSettingError:
        return False
    if desc.env_overridden():
        return True
    row = SqlSiteSettingRepository(session).get(key)
    return row is not None and bool(row.value)


def _serialize(
    desc, *, value: Any, is_set: bool | None = None, session: Session | None = None
) -> SettingResponse:
    type_repr = (
        "list[int]"
        if str(desc.type).startswith("list[int]")
        else (
            "list[str]"
            if str(desc.type).startswith("list[str]")
            else (str(desc.type).removeprefix("typing.").removeprefix("<class '").removesuffix("'>"))
        )
    )
    # Secret settings must never echo decrypted values in API responses.
    if desc.secret:
        resolved_is_set: bool
        if is_set is not None:
            resolved_is_set = is_set
        elif session is not None:
            resolved_is_set = _secret_is_set(desc.key, session)
        else:
            # Fallback: treat the caller-supplied value as a truthiness probe
            # only (value itself is dropped).
            resolved_is_set = bool(value)
        return SettingResponse(
            key=desc.key,
            scope=desc.scope,
            type=type_repr,
            nullable=desc.nullable,
            default=desc.default,
            value=None,
            is_set=resolved_is_set,
            env_var=desc.resolved_env_var(),
            env_overridden=desc.env_overridden(),
            help_text=desc.help_text,
        )
    return SettingResponse(
        key=desc.key,
        scope=desc.scope,
        type=type_repr,
        nullable=desc.nullable,
        default=desc.default,
        value=value,
        is_set=value is not None if is_set is None else is_set,
        env_var=desc.resolved_env_var(),
        env_overridden=desc.env_overridden(),
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
        if desc.secret:
            # Never decrypt secrets on the read path; just report is_set.
            out.append(_serialize(desc, value=None, session=session))
        else:
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
    if desc.secret:
        # Never decrypt secrets on the read path; just report is_set.
        return _serialize(desc, value=None, session=session)
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
        return _serialize(desc, value=desc.default, is_set=False)

    # Run pre-save validator if registered for this key.
    if not body.force_skip_validation and body.value is not None and desc.secret:
        from compendium.web.routes.admin_settings import _SECRET_VALIDATORS
        validator = _SECRET_VALIDATORS.get(key)
        if validator is not None:
            result = validator(str(body.value))
            if not result.ok:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "validation_failed",
                        "reason": result.reason,
                    },
                )

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

    # For secrets: never echo the written value back.  For non-secrets: echo
    # the caller-supplied value (fresh-session re-read would still see the
    # pre-write state because the DI wrapper commits after response build).
    if desc.secret:
        is_set = body.value is not None and body.value != ""
        return _serialize(desc, value=None, is_set=is_set)
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
    # is_set=False: the row was just deleted; env_overridden is still truthful
    # via desc.env_overridden() inside _serialize, but we set is_set explicitly
    # because the env case means the default was never applied in the first place.
    return _serialize(desc, value=desc.default, is_set=False)
