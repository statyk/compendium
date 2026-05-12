"""Web UI routes for bulk import/export of catalog data."""

from __future__ import annotations

import io
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
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
    ImportService,
    decode_text_bytes,
)
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()


def _make_importer(session: Session, user: AppUser) -> ImportService:
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
        actor=user,
        source="web",
    )


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
            "report": None,
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

    ctx = {
        "request": request,
        "user": user,
        "media_types": media_types,
        "branches": branches,
        "mode_choices": _MODE_CHOICES,
        "report": None,
        "error": None,
        "gb_quota_warning": _gb_quota_warning(),
    }
    try:
        mode_enum = ImportMode(mode)
    except ValueError:
        ctx["error"] = f"Unknown mode '{mode}'. Valid: {_MODE_CHOICES}"
        return _render("admin/import.html", request, ctx, status_code=400)

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
            ctx["error"] = exc.detail
            return _render("admin/import.html", request, ctx, status_code=413)
        raise
    importer = _make_importer(session, user)
    replaced = 0
    try:
        if format in ("csv", "librarything", "goodreads"):
            try:
                text, replaced = decode_text_bytes(data, strict=strict_encoding_bool)
            except UnicodeDecodeError as exc:
                ctx["error"] = (
                    f"File is not valid UTF-8: {exc}. Uncheck "
                    "'strict encoding' to import anyway."
                )
                return _render("admin/import.html", request, ctx, status_code=400)
            stream = io.StringIO(text)
            if format == "csv":
                report = importer.import_csv(stream, options, filename=file.filename)
            elif format == "librarything":
                report = importer.import_librarything(
                    stream, options, filename=file.filename
                )
            else:
                report = importer.import_goodreads(
                    stream, options, filename=file.filename
                )
        elif format == "marcxml" or (
            format == "marc"
            and file.filename
            and file.filename.lower().endswith((".xml", ".marcxml"))
        ):
            report = importer.import_marcxml(
                io.BytesIO(data), options, filename=file.filename
            )
        elif format == "marc":
            report = importer.import_marc(
                io.BytesIO(data), options, filename=file.filename
            )
        else:
            ctx["error"] = (
                f"Unknown format '{format}'. Valid: csv, goodreads, librarything, marc, marcxml"
            )
            return _render("admin/import.html", request, ctx, status_code=400)
    except ValidationError as exc:
        ctx["error"] = str(exc)
        return _render("admin/import.html", request, ctx, status_code=400)

    session.commit()
    importer.flush_metadata_cache()

    if replaced:
        report.warnings.insert(
            0,
            f"Decoded with {replaced} byte replacement(s); "
            "file is not clean UTF-8.",
        )
    ctx["report"] = report
    return _render("admin/import.html", request, ctx)


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
