"""Web UI for editing site_setting overrides.

Five admin pages — three under Admin (librarian-tier) and two under System
(``system.manage``). Each page renders a generic form built from descriptor
metadata; submitting writes via ``set_site_setting`` (with audit) and either
shows the page again with a success message or redirects.

The "⚠ Overridden by env var" indicator appears per-row when the
corresponding env var is currently set; in that case the input is disabled
and writes are accepted but won't take effect on read until the env var is
cleared.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.services.audit import AuditService
from compendium.services.settings_registry import (
    SettingValidationError,
    UnknownSettingError,
    all_descriptors,
    get_descriptor,
    parse,
)
from compendium.services.site_settings import (
    delete_site_setting,
    get_site_setting,
    set_site_setting,
)
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Page → setting-key bundles. Order in the list = order in the form.
# ---------------------------------------------------------------------------

_PAGES: dict[str, dict[str, Any]] = {
    "general": {
        "title": "General",
        "scope_perm": "patron.manage",
        "intro": (
            "Library identity and visitor experience. Changes apply on "
            "the next page render — no restart required."
        ),
        "keys": ["library_name", "default_theme", "guest_search_enabled"],
    },
    "circulation": {
        "title": "Circulation defaults",
        "scope_perm": "patron.manage",
        "intro": (
            "Fines, currency display, hold expiry, and overdue-notice "
            "thresholds. These act as defaults; per-policy overrides on "
            "individual loan policies take precedence."
        ),
        "keys": [
            "currency_symbol",
            "currency_symbol_position",
            "fine_block_threshold_cents",
            "fine_block_holds",
            "hold_expiry_days",
            "hold_pickup_days",
            "due_soon_days_before",
            "overdue_tiers",
        ],
    },
    "kiosk": {
        "title": "Self-checkout kiosk",
        "scope_perm": "patron.manage",
        "intro": "Behavior of the public-facing /ui/kiosk self-checkout UI.",
        "keys": ["kiosk_idle_timeout_seconds"],
    },
}

_SYSTEM_PAGES: dict[str, dict[str, Any]] = {
    "smtp": {
        "title": "SMTP / email delivery",
        "scope_perm": "system.manage",
        "intro": (
            "Outbound email configuration for hold-ready, due-soon, and "
            "overdue notices. The SMTP password remains environment-only "
            "(set COMPENDIUM_SMTP_PASSWORD); other knobs are editable here."
        ),
        "keys": [
            "smtp_host",
            "smtp_port",
            "smtp_username",
            "smtp_use_starttls",
            "smtp_use_ssl",
            "smtp_from_address",
            "smtp_from_name",
        ],
    },
    "retention": {
        "title": "Retention & batch sizes",
        "scope_perm": "system.manage",
        "intro": (
            "How much history Compendium keeps and how many notifications "
            "the cron-invoked drainer ships per pass."
        ),
        "keys": [
            "notifications_batch_size",
            "notifications_max_attempts",
            "notification_retention_days",
            "audit_retention_days",
        ],
    },
}

_ALL_PAGES = {**_PAGES, **_SYSTEM_PAGES}


def _audit_svc(session: Session) -> AuditService:
    return AuditService(SqlAuditLogRepository(session))


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(
        request, name, ctx_clean, status_code=status_code
    )
    if fresh:
        set_csrf_cookie(resp, fresh, get_settings().jwt_secret_key)
    return resp


def _build_rows(keys: list[str]) -> list[dict[str, Any]]:
    """Hydrate descriptor + current value + env-override flag for each key."""
    rows = []
    for key in keys:
        desc = get_descriptor(key)
        env_var = desc.resolved_env_var()
        env_overridden = os.environ.get(env_var) is not None
        try:
            value = get_site_setting(key)
        except SettingValidationError:
            value = desc.default
        # Form-rendering helpers
        type_repr = _type_repr(desc.type)
        choices = _literal_choices(desc.type)
        rows.append(
            {
                "key": key,
                "desc": desc,
                "value": value,
                "value_str": _to_form_string(value, desc.type),
                "env_var": env_var,
                "env_overridden": env_overridden,
                "type_repr": type_repr,
                "choices": choices,
                "is_bool": desc.type is bool,
                "is_literal": choices is not None,
                "is_int": desc.type is int,
                "is_list": str(desc.type).startswith("list["),
            }
        )
    return rows


def _type_repr(t: Any) -> str:
    s = str(t)
    if s.startswith("list[int]"):
        return "list of integers"
    if s.startswith("list[str]"):
        return "list of strings"
    if s.startswith("typing.Literal"):
        return "choice"
    if t is bool:
        return "true / false"
    if t is int:
        return "integer"
    if t is str:
        return "text"
    return s


def _literal_choices(t: Any) -> list[str] | None:
    from typing import Literal, get_args, get_origin

    if get_origin(t) is Literal:
        return list(get_args(t))
    return None


def _to_form_string(value: Any, t: Any) -> str:
    if value is None:
        return ""
    if t is bool:
        return "true" if value else "false"
    if str(t).startswith("list["):
        if isinstance(value, list):
            return ", ".join(str(x) for x in value)
        return str(value)
    return str(value)


# ---------------------------------------------------------------------------
# Concrete endpoints. We keep them explicit (rather than one parameterized
# route) so the FastAPI permission dependency is checked against the right
# scope per page.
# ---------------------------------------------------------------------------


def _show_page(
    page_key: str,
    page_meta: dict[str, Any],
    request: Request,
    message: str | None,
    error: str | None,
    user: AppUser,
):
    rows = _build_rows(page_meta["keys"])
    return _render(
        "admin/settings.html",
        request,
        {
            "request": request,
            "user": user,
            "page_key": page_key,
            "title": page_meta["title"],
            "intro": page_meta["intro"],
            "rows": rows,
            "is_system": page_key in _SYSTEM_PAGES,
            "message": message,
            "error": error,
        },
    )


def _apply_form(
    page_key: str,
    page_meta: dict[str, Any],
    request: Request,
    form_values: dict[str, str],
    reset_keys: list[str],
    session: Session,
    user: AppUser,
) -> tuple[str | None, str | None]:
    """Apply submitted form values + resets. Returns (message, error)."""
    audit = _audit_svc(session)
    errors: list[str] = []
    changed = 0

    for key in reset_keys:
        if key not in page_meta["keys"]:
            continue
        if delete_site_setting(
            key, session=session, audit_svc=audit, actor=user, source="web"
        ):
            changed += 1

    for key in page_meta["keys"]:
        if key in reset_keys:
            continue
        try:
            desc = get_descriptor(key)
        except UnknownSettingError:
            continue
        # Skip keys whose env var is set — write would be silently masked.
        if os.environ.get(desc.resolved_env_var()) is not None:
            continue
        raw = form_values.get(key, "")
        # Bool: missing checkbox = false; otherwise treat the form value
        # as the descriptor parses it.
        if desc.type is bool:
            raw = "true" if key in form_values else "false"
        try:
            parsed = parse(desc, raw)
            set_site_setting(
                key,
                parsed,
                session=session,
                updated_by_id=user.id,
                audit_svc=audit,
                actor=user,
                source="web",
            )
            changed += 1
        except SettingValidationError as exc:
            errors.append(f"{key}: {exc}")

    if errors:
        return (None, "; ".join(errors))
    if changed == 0:
        return ("Nothing to save.", None)
    return (f"Saved {changed} change(s).", None)


def _post_handler(
    page_key: str,
    page_meta: dict[str, Any],
    request: Request,
    form_values: dict[str, str],
    reset_keys: list[str],
    session: Session,
    user: AppUser,
):
    msg, err = _apply_form(
        page_key, page_meta, request, form_values, reset_keys, session, user
    )
    target = (
        f"/ui/admin/system/{page_key}"
        if page_key in _SYSTEM_PAGES
        else f"/ui/admin/settings/{page_key}"
    )
    qs = (
        f"?message={quote(msg)}"
        if msg
        else (f"?error={quote(err)}" if err else "")
    )
    return RedirectResponse(target + qs, status_code=303)


# ── Librarian-tier pages ──────────────────────────────────────────────────


@router.get("/admin/settings/general")
def general_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("patron.manage")),
):
    return _show_page("general", _PAGES["general"], request, message, error, user)


@router.post("/admin/settings/general")
async def general_post(
    request: Request,
    user: AppUser = Depends(require_web_permission("patron.manage")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    reset_keys = form.getlist("reset")
    form_values = {k: v for k, v in form.items() if k not in ("csrf_token", "reset")}
    return _post_handler(
        "general", _PAGES["general"], request, form_values, reset_keys, session, user
    )


@router.get("/admin/settings/circulation")
def circulation_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("patron.manage")),
):
    return _show_page(
        "circulation", _PAGES["circulation"], request, message, error, user
    )


@router.post("/admin/settings/circulation")
async def circulation_post(
    request: Request,
    user: AppUser = Depends(require_web_permission("patron.manage")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    reset_keys = form.getlist("reset")
    form_values = {k: v for k, v in form.items() if k not in ("csrf_token", "reset")}
    return _post_handler(
        "circulation",
        _PAGES["circulation"],
        request,
        form_values,
        reset_keys,
        session,
        user,
    )


@router.get("/admin/settings/kiosk")
def kiosk_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("patron.manage")),
):
    return _show_page("kiosk", _PAGES["kiosk"], request, message, error, user)


@router.post("/admin/settings/kiosk")
async def kiosk_post(
    request: Request,
    user: AppUser = Depends(require_web_permission("patron.manage")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    reset_keys = form.getlist("reset")
    form_values = {k: v for k, v in form.items() if k not in ("csrf_token", "reset")}
    return _post_handler(
        "kiosk", _PAGES["kiosk"], request, form_values, reset_keys, session, user
    )


# ── System-tier pages ─────────────────────────────────────────────────────


@router.get("/admin/system/smtp")
def smtp_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("system.manage")),
):
    return _show_page("smtp", _SYSTEM_PAGES["smtp"], request, message, error, user)


@router.post("/admin/system/smtp")
async def smtp_post(
    request: Request,
    user: AppUser = Depends(require_web_permission("system.manage")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    reset_keys = form.getlist("reset")
    form_values = {k: v for k, v in form.items() if k not in ("csrf_token", "reset")}
    return _post_handler(
        "smtp", _SYSTEM_PAGES["smtp"], request, form_values, reset_keys, session, user
    )


@router.get("/admin/system/retention")
def retention_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("system.manage")),
):
    return _show_page(
        "retention", _SYSTEM_PAGES["retention"], request, message, error, user
    )


@router.post("/admin/system/retention")
async def retention_post(
    request: Request,
    user: AppUser = Depends(require_web_permission("system.manage")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    reset_keys = form.getlist("reset")
    form_values = {k: v for k, v in form.items() if k not in ("csrf_token", "reset")}
    return _post_handler(
        "retention",
        _SYSTEM_PAGES["retention"],
        request,
        form_values,
        reset_keys,
        session,
        user,
    )
