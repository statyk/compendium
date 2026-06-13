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

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
from compendium.services.auth import has_permission
from compendium.web.deps import require_web_permission, require_web_user
from compendium.web.jinja import templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Page → setting-key bundles. Order in the list = order in the form.
# ---------------------------------------------------------------------------

_PAGES: dict[str, dict[str, Any]] = {
    "general": {
        "title": "General",
        "scope_perm": "patron.manage",
        "intro": "Library identity and visitor experience. No restart required.",
        "keys": ["library_name", "library_timezone", "default_theme", "guest_search_enabled", "custom_shortcuts"],
    },
    "circulation": {
        "title": "Circulation defaults",
        "scope_perm": "patron.manage",
        "intro": (
            "Default fines, currency, hold expiry, and overdue thresholds. "
            "Per-policy overrides win."
        ),
        "keys": [
            "circulation_scan_isbn_enabled",
            "default_loan_period_days",
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
    "identifiers": {
        "title": "Identifiers & barcodes",
        "scope_perm": "branch.edit",
        "intro": (
            "Barcode format and symbology for newly minted item barcodes and "
            "patron cards. Existing codes are unaffected."
        ),
        "keys": [
            "barcode_format",
            "barcode_default_location_code",
            "barcode_symbology",
        ],
    },
    "labels": {
        "title": "Label defaults",
        "scope_perm": "labels.generate",
        "intro": (
            "Default fields shown on each label kind; staff can still toggle "
            "fields per-generation."
        ),
        "keys": [
            "label_spine_default_fields",
            "label_pocket_default_fields",
            "label_barcode_only_default_fields",
            "label_patron_full_default_fields",
            "label_patron_sticker_default_fields",
        ],
    },
}

_SYSTEM_PAGES: dict[str, dict[str, Any]] = {
    "metadata": {
        "title": "Metadata sources",
        "scope_perm": "system.manage",
        "intro": "Which service supplies book metadata, fallback behavior, and cache lifetime.",
        "keys": [
            "book_metadata_source_preference",
            "google_books_api_key",
            "book_metadata_fallback_enabled",
            "metadata_cache_ttl_days",
            "tmdb_api_key",
        ],
    },
    "smtp": {
        "title": "SMTP / email delivery",
        "scope_perm": "system.manage",
        "intro": "Outbound email for hold-ready, due-soon, and overdue notices.",
        "keys": [
            "smtp_host",
            "smtp_port",
            "smtp_username",
            "smtp_password",
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
    "security": {
        "title": "Security & rate limiting",
        "scope_perm": "system.manage",
        "intro": "Per-identity login throttling. See each field for details.",
        "keys": [
            "login_max_failures",
            "login_failure_window_seconds",
            "password_min_length",
            "bcrypt_rounds",
        ],
    },
}

_ALL_PAGES = {**_PAGES, **_SYSTEM_PAGES}

# Flat ordered list consumed by the settings sidebar and hub.
SETTINGS_PAGES: list[dict[str, Any]] = [
    {
        "slug": slug,
        "url": (
            f"/ui/admin/system/{slug}"
            if slug in _SYSTEM_PAGES
            else f"/ui/admin/settings/{slug}"
        ),
        "title": meta["title"],
        "intro": meta["intro"],
        "scope_perm": meta["scope_perm"],
        "tier": "system" if slug in _SYSTEM_PAGES else "librarian",
    }
    for slug, meta in _ALL_PAGES.items()
]


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
        set_csrf_cookie(resp, fresh)
    return resp


def _build_rows(keys: list[str], session: Session | None = None) -> list[dict[str, Any]]:
    """Hydrate descriptor + current value + env-override flag for each key.

    Secret descriptors get a password-style row instead of a value-bearing one;
    their stored value is never read or echoed (only a db_set boolean).
    """
    rows = []
    for key in keys:
        desc = get_descriptor(key)
        env_var = desc.resolved_env_var()
        env_overridden = desc.env_overridden()
        if desc.secret:
            db_set = False
            if session is not None:
                from compendium.repositories.sql.site_setting_repository import (
                    SqlSiteSettingRepository,
                )
                db_row = SqlSiteSettingRepository(session).get(desc.key)
                db_set = db_row is not None and bool(db_row.value)
            rows.append(
                {
                    "key": key,
                    "display_name": desc.resolved_display_name(),
                    "desc": desc,
                    "env_var": env_var,
                    "env_overridden": env_overridden,
                    "is_secret": True,
                    "db_set": db_set,
                }
            )
            continue
        try:
            value = get_site_setting(key)
        except SettingValidationError:
            value = desc.default
        type_repr = _type_repr(desc.type)
        choices = _literal_choices(desc.type)
        hint = desc.availability_hint() if desc.availability_hint is not None else None
        unavailable_choices: set[str] = set(hint.unavailable_choices) if hint else set()
        availability_warning: str | None = hint.warning if hint else None
        rows.append(
            {
                "key": key,
                "display_name": desc.resolved_display_name(),
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
                "unavailable_choices": unavailable_choices,
                "availability_warning": availability_warning,
                "is_secret": False,
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
    session: Session | None = None,
    extra_ctx: dict[str, Any] | None = None,
):
    rows = _build_rows(page_meta["keys"], session=session)
    has_secrets = any(r.get("is_secret") for r in rows)
    ctx: dict[str, Any] = {
        "request": request,
        "user": user,
        "page_key": page_key,
        "title": page_meta["title"],
        "intro": page_meta["intro"],
        "rows": rows,
        "is_system": page_key in _SYSTEM_PAGES,
        "message": message,
        "error": error,
        "has_secrets": has_secrets,
        "key_configured": False,
        "canary_mismatch": False,
        "settings_pages": SETTINGS_PAGES,
    }
    if has_secrets and session is not None:
        ctx.update(_secrets_banner_ctx(session))
    if extra_ctx:
        ctx.update(extra_ctx)
    return _render("admin/settings.html", request, ctx)


def _apply_form(
    page_key: str,
    page_meta: dict[str, Any],
    request: Request,
    form: Any,
    reset_keys: list[str],
    clear_keys: list[str],
    session: Session,
    user: AppUser,
) -> tuple[str | None, str | None]:
    """Apply submitted settings + resets + inline secrets. Returns (message, error)."""
    audit = _audit_svc(session)
    errors: list[str] = []
    changed = 0

    secret_keys = [k for k in page_meta["keys"] if get_descriptor(k).secret]
    normal_keys = [k for k in page_meta["keys"] if k not in secret_keys]

    for key in reset_keys:
        if key not in normal_keys:
            continue
        if delete_site_setting(
            key, session=session, audit_svc=audit, actor=user, source="web"
        ):
            changed += 1

    for key in normal_keys:
        if key in reset_keys:
            continue
        try:
            desc = get_descriptor(key)
        except UnknownSettingError:
            continue
        if desc.env_overridden():
            continue
        raw = form.get(key, "")
        if desc.type is bool:
            raw = "true" if key in form else "false"
        try:
            parsed = parse(desc, raw)
            set_site_setting(
                key, parsed, session=session, updated_by_id=user.id,
                audit_svc=audit, actor=user, source="web",
            )
            changed += 1
        except SettingValidationError as exc:
            errors.append(f"{key}: {exc}")

    if secret_keys:
        s_changed, s_errors = _apply_secret_fields(
            secret_keys, form, clear_keys, session, user, audit
        )
        changed += s_changed
        errors.extend(s_errors)

    if errors:
        return (None, "; ".join(errors))
    if changed == 0:
        return ("Nothing to save.", None)
    return (f"Saved {changed} change(s).", None)


def _post_handler(
    page_key: str,
    page_meta: dict[str, Any],
    request: Request,
    form: Any,
    reset_keys: list[str],
    clear_keys: list[str],
    session: Session,
    user: AppUser,
):
    msg, err = _apply_form(
        page_key, page_meta, request, form, reset_keys, clear_keys, session, user
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


# ── Settings hub ──────────────────────────────────────────────────────────


def _require_settings_access(
    request: Request,
    user: AppUser = Depends(require_web_user),
) -> AppUser:
    if not any(
        has_permission(user.role.permissions, p)
        for p in ("patron.manage", "branch.edit", "system.manage")
    ):
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


@router.get("/admin/settings")
def settings_hub_get(
    request: Request,
    user: AppUser = Depends(_require_settings_access),
):
    pages = [
        p for p in SETTINGS_PAGES
        if has_permission(user.role.permissions, p["scope_perm"])
    ]
    ctx: dict[str, Any] = {
        "request": request,
        "user": user,
        "pages": pages,
        "settings_pages": SETTINGS_PAGES,
    }
    return _render("admin/settings_index.html", request, ctx)


# ── Librarian-tier pages ──────────────────────────────────────────────────


@router.get("/admin/settings/general")
def general_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("patron.manage")),
    session: Session = Depends(get_session),
):
    return _show_page(
        "general", _PAGES["general"], request, message, error, user, session=session
    )


@router.post("/admin/settings/general")
async def general_post(
    request: Request,
    user: AppUser = Depends(require_web_permission("patron.manage")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    reset_keys = form.getlist("reset")
    clear_keys = form.getlist("clear")
    return _post_handler(
        "general", _PAGES["general"], request, form, reset_keys, clear_keys, session, user
    )


@router.get("/admin/settings/circulation")
def circulation_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("patron.manage")),
    session: Session = Depends(get_session),
):
    return _show_page(
        "circulation", _PAGES["circulation"], request, message, error, user, session=session
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
    clear_keys = form.getlist("clear")
    return _post_handler(
        "circulation", _PAGES["circulation"], request, form, reset_keys, clear_keys, session, user
    )


@router.get("/admin/settings/kiosk")
def kiosk_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("patron.manage")),
    session: Session = Depends(get_session),
):
    return _show_page(
        "kiosk", _PAGES["kiosk"], request, message, error, user, session=session
    )


@router.post("/admin/settings/kiosk")
async def kiosk_post(
    request: Request,
    user: AppUser = Depends(require_web_permission("patron.manage")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    reset_keys = form.getlist("reset")
    clear_keys = form.getlist("clear")
    return _post_handler(
        "kiosk", _PAGES["kiosk"], request, form, reset_keys, clear_keys, session, user
    )


@router.get("/admin/settings/identifiers")
def identifiers_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("branch.edit")),
    session: Session = Depends(get_session),
):
    return _show_page(
        "identifiers", _PAGES["identifiers"], request, message, error, user, session=session
    )


@router.post("/admin/settings/identifiers")
async def identifiers_post(
    request: Request,
    user: AppUser = Depends(require_web_permission("branch.edit")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    reset_keys = form.getlist("reset")
    clear_keys = form.getlist("clear")
    return _post_handler(
        "identifiers", _PAGES["identifiers"], request, form, reset_keys, clear_keys, session, user
    )


@router.get("/admin/settings/labels")
def labels_settings_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("labels.generate")),
    session: Session = Depends(get_session),
):
    return _show_page(
        "labels", _PAGES["labels"], request, message, error, user, session=session
    )


@router.post("/admin/settings/labels")
async def labels_settings_post(
    request: Request,
    user: AppUser = Depends(require_web_permission("labels.generate")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    reset_keys = form.getlist("reset")
    clear_keys = form.getlist("clear")
    return _post_handler(
        "labels", _PAGES["labels"], request, form, reset_keys, clear_keys, session, user
    )


# ── System-tier pages ─────────────────────────────────────────────────────


@router.get("/admin/system/smtp")
def smtp_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("system.manage")),
    session: Session = Depends(get_session),
):
    return _show_page(
        "smtp", _SYSTEM_PAGES["smtp"], request, message, error, user, session=session
    )


@router.post("/admin/system/smtp")
async def smtp_post(
    request: Request,
    user: AppUser = Depends(require_web_permission("system.manage")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    reset_keys = form.getlist("reset")
    clear_keys = form.getlist("clear")
    return _post_handler(
        "smtp", _SYSTEM_PAGES["smtp"], request, form, reset_keys, clear_keys, session, user
    )


@router.get("/admin/system/retention")
def retention_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("system.manage")),
    session: Session = Depends(get_session),
):
    return _show_page(
        "retention", _SYSTEM_PAGES["retention"], request, message, error, user, session=session
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
    clear_keys = form.getlist("clear")
    return _post_handler(
        "retention", _SYSTEM_PAGES["retention"], request, form, reset_keys, clear_keys,
        session, user,
    )


@router.get("/admin/system/security")
def security_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("system.manage")),
    session: Session = Depends(get_session),
):
    return _show_page(
        "security", _SYSTEM_PAGES["security"], request, message, error, user, session=session
    )


@router.post("/admin/system/security")
async def security_post(
    request: Request,
    user: AppUser = Depends(require_web_permission("system.manage")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    reset_keys = form.getlist("reset")
    clear_keys = form.getlist("clear")
    return _post_handler(
        "security", _SYSTEM_PAGES["security"], request, form, reset_keys, clear_keys, session, user
    )


@router.get("/admin/system/metadata")
def metadata_get(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission("system.manage")),
    session: Session = Depends(get_session),
):
    from compendium.services.metadata import get_book_primary_adapter_name

    # active_book_source feeds the metadata-source widget (rendered in the
    # source-preference template branch); always computed for the metadata page.
    active = get_book_primary_adapter_name()
    active_label = "Google Books" if active == "googlebooks" else "Open Library"
    return _show_page(
        "metadata", _SYSTEM_PAGES["metadata"], request, message, error, user,
        session=session,
        extra_ctx={"active_book_source": active_label},
    )


@router.post("/admin/system/metadata")
async def metadata_post(
    request: Request,
    user: AppUser = Depends(require_web_permission("system.manage")),
    session: Session = Depends(get_session),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    reset_keys = form.getlist("reset")
    clear_keys = form.getlist("clear")
    return _post_handler(
        "metadata", _SYSTEM_PAGES["metadata"], request, form, reset_keys, clear_keys, session, user
    )


def _secrets_banner_ctx(session) -> dict[str, Any]:
    """Return key_configured / canary_mismatch flags for the secrets banner."""
    from compendium.services.secrets import CanaryResult, check_canary, secret_key_configured

    key_configured = secret_key_configured()
    canary = check_canary(session) if key_configured else CanaryResult.NO_KEY
    return {
        "key_configured": key_configured,
        "canary_mismatch": canary.value == "mismatch",
    }


# ── Secrets page ──────────────────────────────────────────────────────────────


@router.get("/admin/system/secrets")
def secrets_get(
    request: Request,
    user: AppUser = Depends(require_web_permission("system.manage")),
):
    # API keys now live on their respective settings pages.
    return RedirectResponse("/ui/admin/system/metadata", status_code=301)


_SECRET_VALIDATORS: dict[str, Any] = {}


def _register_secret_validators() -> None:
    """Populate the secret pre-save validator registry.

    Kept as a function (called once at import time) so validators can import
    service code without creating import cycles at module load.
    """
    from compendium.services.metadata import validate_google_books_key

    _SECRET_VALIDATORS["google_books_api_key"] = validate_google_books_key


_register_secret_validators()


def _apply_secret_fields(
    secret_keys: list[str],
    form: Any,
    clear_keys: list[str],
    session: Session,
    user: AppUser,
    audit: AuditService,
) -> tuple[int, list[str]]:
    """Apply secret sets/clears for the given keys. Returns (changed, errors).

    `form` is the starlette FormData (supports .get). Validation failures are
    returned as error strings (the page surfaces them via ?error=).
    """
    from compendium.services.secrets import SecretKeyMismatchError, SecretKeyMissingError

    secret_descs = {d.key: d for d in all_descriptors() if d.secret}
    changed = 0
    errors: list[str] = []

    for key in clear_keys:
        if key not in secret_keys or key not in secret_descs:
            continue
        if delete_site_setting(key, session=session, audit_svc=audit, actor=user, source="web"):
            changed += 1

    for key in secret_keys:
        desc = secret_descs.get(key)
        if desc is None or key in clear_keys or desc.env_overridden():
            continue
        raw = form.get(key, "").strip()
        if not raw:
            continue
        validator = _SECRET_VALIDATORS.get(key)
        override_field = f"override_validation_{key}"
        if validator is not None and not form.get(override_field):
            result = validator(raw)
            if not result.ok:
                errors.append(
                    f"{desc.resolved_display_name()}: "
                    f"{result.reason or 'validation failed'}"
                )
                continue
            if result.warning:
                errors.append(f"{desc.resolved_display_name()}: {result.warning}")
        try:
            set_site_setting(
                key, raw, session=session, updated_by_id=user.id,
                audit_svc=audit, actor=user, source="web",
            )
            changed += 1
        except (SecretKeyMissingError, SecretKeyMismatchError) as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(f"{key}: {exc}")

    return changed, errors


@router.post("/admin/system/secrets")
async def secrets_post(
    request: Request,
    user: AppUser = Depends(require_web_permission("system.manage")),
    session: Session = Depends(get_session),
):
    _SECRETS_REDIRECT_WHITELIST = {
        "/ui/admin/system/metadata",
        "/ui/admin/system/smtp",
    }
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    redirect_to = form.get("redirect_to", "").strip()
    if redirect_to not in _SECRETS_REDIRECT_WHITELIST:
        redirect_to = "/ui/admin/system/metadata"

    secret_keys = [d.key for d in all_descriptors() if d.secret]
    clear_keys = form.getlist("clear")
    audit = _audit_svc(session)
    changed, errors = _apply_secret_fields(
        secret_keys, form, clear_keys, session, user, audit
    )

    if errors:
        qs = f"?error={quote('; '.join(errors))}"
    elif changed:
        qs = f"?message={quote(f'Saved {changed} change(s).')}"
    else:
        qs = "?message=Nothing+to+save."
    return RedirectResponse(redirect_to + qs, status_code=303)
