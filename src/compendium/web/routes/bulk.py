"""Web UI routes for bulk import/export of catalog data."""

from __future__ import annotations

import collections
import io
import threading
import uuid
from dataclasses import dataclass, field as _field
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from compendium.api.uploads import read_upload_bounded
from compendium.config.settings import Settings
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import ValidationError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.repositories.sql.counters import SqlCounterRepository
from compendium.services.catalog import CatalogService
from compendium.services.import_export import (
    ExportFilters,
    ExportService,
    ImportMode,
    ImportOptions,
    ImportReport,
    ImportService,
    decode_text_bytes,
)
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

# ---------------------------------------------------------------------------
# In-memory job store for background imports
# ---------------------------------------------------------------------------

@dataclass
class _JobState:
    status: str          # "pending" | "running" | "done" | "failed"
    filename: str | None
    format: str
    user_id: int         # owner; only this user can poll the job
    processed_rows: int = 0
    created_works: int = 0
    added_copies: int = 0
    skipped_duplicates: int = 0
    enriched_rows: int = 0
    report: ImportReport | None = None
    error: str | None = None
    # Retained ONLY for dry-run jobs so they can be re-applied in one click.
    dry_run: bool = False
    payload: str | bytes | None = None
    options: ImportOptions | None = None
    replaced: int = 0
    lock: threading.Lock = _field(default_factory=threading.Lock, repr=False)


_JOBS: collections.OrderedDict[str, _JobState] = collections.OrderedDict()
_JOBS_LOCK = threading.Lock()
_MAX_JOBS = 200


def _create_job(state: _JobState) -> str:
    job_id = str(uuid.uuid4())
    with _JOBS_LOCK:
        _JOBS[job_id] = state
        while len(_JOBS) > _MAX_JOBS:
            _JOBS.popitem(last=False)
    return job_id


def _get_job(job_id: str) -> _JobState | None:
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


# ---------------------------------------------------------------------------
# Background import thread
# ---------------------------------------------------------------------------

def _make_importer_for_session(session: Session, actor: AppUser | None) -> ImportService:
    catalog = CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=None,
        counter_repo=SqlCounterRepository(session),
    )
    return ImportService(
        session=session,
        catalog=catalog,
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="web",
    )


def _run_import_job(
    state: _JobState,
    fmt: str,
    payload: str | bytes,
    options: ImportOptions,
    replaced: int,
) -> None:
    """payload is str for csv/librarything/goodreads, bytes for marc/marcxml."""

    def on_progress(report: ImportReport) -> None:
        with state.lock:
            state.processed_rows = report.total_rows
            state.created_works = report.created_works
            state.added_copies = report.added_copies
            state.skipped_duplicates = report.skipped_duplicates
            state.enriched_rows = report.enriched_rows

    with state.lock:
        state.status = "running"

    importer: ImportService | None = None
    report: ImportReport | None = None
    try:
        from compendium.db.session import session_scope

        with session_scope() as session:
            actor = session.get(AppUser, state.user_id)
            importer = _make_importer_for_session(session, actor)
            if fmt == "csv":
                report = importer.import_csv(
                    io.StringIO(payload),  # type: ignore[arg-type]
                    options,
                    filename=state.filename,
                    on_progress=on_progress,
                )
            elif fmt == "librarything":
                report = importer.import_librarything(
                    io.StringIO(payload),  # type: ignore[arg-type]
                    options,
                    filename=state.filename,
                    on_progress=on_progress,
                )
            elif fmt == "goodreads":
                report = importer.import_goodreads(
                    io.StringIO(payload),  # type: ignore[arg-type]
                    options,
                    filename=state.filename,
                    on_progress=on_progress,
                )
            elif fmt == "marcxml":
                report = importer.import_marcxml(
                    io.BytesIO(payload),  # type: ignore[arg-type]
                    options,
                    filename=state.filename,
                    on_progress=on_progress,
                )
            else:  # marc
                report = importer.import_marc(
                    io.BytesIO(payload),  # type: ignore[arg-type]
                    options,
                    filename=state.filename,
                    on_progress=on_progress,
                )
            # session_scope commits on normal exit

        if replaced:
            report.warnings.insert(
                0,
                f"Decoded with {replaced} byte replacement(s); "
                "file is not clean UTF-8.",
            )
        with state.lock:
            state.status = "done"
            state.report = report
            state.processed_rows = report.total_rows
            state.created_works = report.created_works
            state.added_copies = report.added_copies
            state.skipped_duplicates = report.skipped_duplicates
            state.enriched_rows = report.enriched_rows
    except Exception as exc:
        with state.lock:
            state.status = "failed"
            state.error = str(exc)
    finally:
        # Flush cache entries collected during enrichment regardless of outcome;
        # upstream API responses already fetched are valid and worth keeping.
        if importer is not None:
            try:
                importer.flush_metadata_cache()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------

def _make_importer(session: Session, user: AppUser) -> ImportService:
    return _make_importer_for_session(session, user)


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean, status_code=status_code)
    if fresh:
        set_csrf_cookie(resp, fresh)
    return resp


_MODE_CHOICES = [m.value for m in ImportMode]


def _gb_quota_warning() -> str | None:
    """Return a warning message if Google Books quota is currently exhausted, else None."""
    from compendium.services.metadata import get_book_primary_adapter_name, is_gb_quota_exhausted

    if get_book_primary_adapter_name() == "googlebooks" and is_gb_quota_exhausted():
        return (
            "Google Books daily quota exhausted (resets ~24 h after first hit). "
            "Enriched imports will use Open Library only until the quota resets."
        )
    return None


# ---------------------------------------------------------------------------
# Import form + async submission
# ---------------------------------------------------------------------------

@router.get("/admin/import")
def import_form(
    request: Request,
    user: AppUser = Depends(require_web_permission("catalog.import")),
    session: Session = Depends(get_session),
):
    media_types = SqlMediaTypeRepository(session).list()
    branches = SqlBranchRepository(session).list()
    return _render(
        "admin/import.html",
        request,
        {
            "request": request,
            "user": user,
            "media_types": media_types,
            "branches": branches,
            "mode_choices": _MODE_CHOICES,
            "error": None,
            "gb_quota_warning": _gb_quota_warning(),
        },
    )


@router.post("/admin/import")
async def import_submit(
    request: Request,
    format: str = Form(...),
    file: UploadFile = File(...),
    mode: str = Form("append"),
    dry_run: str | None = Form(None),
    default_branch: str | None = Form(None),
    default_media_type: str | None = Form(None),
    enrich: str | None = Form(None),
    preserve_barcodes: str | None = Form(None),
    strict_encoding: str | None = Form(None),
    csrf_token: str = Form(...),
    content_length: int | None = Header(default=None, alias="content-length"),
    settings: Settings = Depends(get_settings),
    user: AppUser = Depends(require_web_permission("catalog.import")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    media_types = SqlMediaTypeRepository(session).list()
    branches = SqlBranchRepository(session).list()

    def _err(msg: str, code: int = 400):
        return _render(
            "admin/import.html",
            request,
            {
                "request": request,
                "user": user,
                "media_types": media_types,
                "branches": branches,
                "mode_choices": _MODE_CHOICES,
                "error": msg,
                "gb_quota_warning": _gb_quota_warning(),
            },
            status_code=code,
        )

    try:
        mode_enum = ImportMode(mode)
    except ValueError:
        return _err(f"Unknown mode '{mode}'. Valid: {_MODE_CHOICES}")

    strict_encoding_bool = bool(strict_encoding)
    options = ImportOptions(
        mode=mode_enum,
        dry_run=bool(dry_run),
        default_branch_code=default_branch or None,
        default_media_type=default_media_type or None,
        enrich_from_external=bool(enrich),
        preserve_barcodes=bool(preserve_barcodes),
        strict_encoding=strict_encoding_bool,
    )

    try:
        data = await read_upload_bounded(
            file, cap=settings.max_upload_bytes, content_length=content_length
        )
    except HTTPException as exc:
        if exc.status_code == 413:
            return _err(exc.detail, code=413)
        raise

    # Normalize marcxml by filename extension (matches old route logic).
    fmt = format
    if fmt == "marc" and file.filename and file.filename.lower().endswith((".xml", ".marcxml")):
        fmt = "marcxml"

    # Decode text formats synchronously so encoding errors surface immediately.
    replaced = 0
    if fmt in ("csv", "librarything", "goodreads"):
        try:
            payload_str, replaced = decode_text_bytes(data, strict=strict_encoding_bool)
        except UnicodeDecodeError as exc:
            return _err(
                f"File is not valid UTF-8: {exc}. Uncheck "
                "'strict encoding' to import anyway."
            )
        payload: str | bytes = payload_str
    else:
        payload = data

    is_dry = bool(dry_run)
    state = _JobState(
        status="pending",
        filename=file.filename,
        format=fmt,
        user_id=user.id,
        dry_run=is_dry,
        payload=payload if is_dry else None,
        options=options if is_dry else None,
        replaced=replaced if is_dry else 0,
    )
    job_id = _create_job(state)

    t = threading.Thread(
        target=_run_import_job,
        args=(state, fmt, payload, options, replaced),
        daemon=True,
    )
    t.start()

    return RedirectResponse(f"/ui/admin/import/jobs/{job_id}", status_code=303)


# ---------------------------------------------------------------------------
# Import job status routes
# ---------------------------------------------------------------------------

@router.get("/admin/import/jobs/{job_id}")
def import_job_page(
    job_id: str,
    request: Request,
    user: AppUser = Depends(require_web_permission("catalog.import")),
):
    state = _get_job(job_id)
    if state is None or state.user_id != user.id:
        raise HTTPException(status_code=404, detail="Import job not found.")
    return _render(
        "admin/import_job.html",
        request,
        {"request": request, "user": user, "job_id": job_id, "state": state},
    )


@router.get("/admin/import/jobs/{job_id}/status")
def import_job_status(
    job_id: str,
    request: Request,
    user: AppUser = Depends(require_web_permission("catalog.import")),
):
    state = _get_job(job_id)
    if state is None or state.user_id != user.id:
        raise HTTPException(status_code=404, detail="Import job not found.")
    # Snapshot under the lock to avoid torn reads.
    with state.lock:
        snap = _JobState(
            status=state.status,
            filename=state.filename,
            format=state.format,
            user_id=state.user_id,
            processed_rows=state.processed_rows,
            created_works=state.created_works,
            added_copies=state.added_copies,
            skipped_duplicates=state.skipped_duplicates,
            enriched_rows=state.enriched_rows,
            report=state.report,
            error=state.error,
        )
    return _render(
        "admin/_import_status_partial.html",
        request,
        {"request": request, "user": user, "job_id": job_id, "state": snap},
    )


@router.post("/admin/import/jobs/{job_id}/apply")
async def import_job_apply(
    job_id: str,
    request: Request,
    user: AppUser = Depends(require_web_permission("catalog.import")),
):
    form = await request.form()
    check_csrf_form(request, form.get("csrf_token", ""))
    state = _get_job(job_id)
    if (
        state is None
        or state.user_id != user.id
        or not state.dry_run
        or state.payload is None
        or state.options is None
    ):
        raise HTTPException(status_code=404, detail="Import job not available to apply.")

    import dataclasses

    apply_options = dataclasses.replace(state.options, dry_run=False)
    new_state = _JobState(
        status="pending",
        filename=state.filename,
        format=state.format,
        user_id=user.id,
        dry_run=False,
    )
    new_id = _create_job(new_state)
    t = threading.Thread(
        target=_run_import_job,
        args=(new_state, state.format, state.payload, apply_options, state.replaced),
        daemon=True,
    )
    t.start()
    return RedirectResponse(f"/ui/admin/import/jobs/{new_id}", status_code=303)


# ---------------------------------------------------------------------------
# Export routes (unchanged)
# ---------------------------------------------------------------------------

@router.get("/admin/export")
def export_form(
    request: Request,
    user: AppUser = Depends(require_web_permission("item.view")),
    session: Session = Depends(get_session),
):
    media_types = SqlMediaTypeRepository(session).list()
    branches = SqlBranchRepository(session).list()
    return _render(
        "admin/export.html",
        request,
        {
            "request": request,
            "user": user,
            "media_types": media_types,
            "branches": branches,
            "error": None,
        },
    )


@router.post("/admin/export")
def export_submit(
    request: Request,
    format: str = Form(...),
    media_type: str | None = Form(None),
    branch: str | None = Form(None),
    since: str | None = Form(None),
    csrf_token: str = Form(...),
    user: AppUser = Depends(require_web_permission("item.view")),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    parsed_since: datetime | None = None
    if since:
        try:
            parsed_since = datetime.fromisoformat(since)
        except ValueError:
            media_types = SqlMediaTypeRepository(session).list()
            branches = SqlBranchRepository(session).list()
            return _render(
                "admin/export.html",
                request,
                {
                    "request": request,
                    "user": user,
                    "media_types": media_types,
                    "branches": branches,
                    "error": f"since must be ISO-8601 (YYYY-MM-DD); got '{since}'",
                },
                status_code=400,
            )
    filters = ExportFilters(
        media_type_code=media_type or None,
        branch_code=branch or None,
        since=parsed_since,
    )
    exporter = ExportService(work_repo=SqlWorkRepository(session))

    if format == "csv":
        buf = io.StringIO()
        exporter.export_csv(buf, filters)
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="compendium.csv"'
            },
        )
    if format == "marc":
        buf_b = io.BytesIO()
        exporter.export_marc(buf_b, filters)
        return Response(
            content=buf_b.getvalue(),
            media_type="application/marc",
            headers={
                "Content-Disposition": 'attachment; filename="compendium.mrc"'
            },
        )
    if format == "marcxml":
        buf_b = io.BytesIO()
        exporter.export_marcxml(buf_b, filters)
        return Response(
            content=buf_b.getvalue(),
            media_type="application/marcxml+xml",
            headers={
                "Content-Disposition": 'attachment; filename="compendium.xml"'
            },
        )
    media_types = SqlMediaTypeRepository(session).list()
    branches = SqlBranchRepository(session).list()
    return _render(
        "admin/export.html",
        request,
        {
            "request": request,
            "user": user,
            "media_types": media_types,
            "branches": branches,
            "error": f"Unknown format '{format}'",
        },
        status_code=400,
    )
