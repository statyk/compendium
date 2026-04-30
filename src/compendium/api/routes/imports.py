"""API endpoints: bulk import + export of catalog data."""

from __future__ import annotations

import getpass
from datetime import datetime

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.uploads import read_upload_bounded
from compendium.config.settings import Settings
from compendium.db.engine import get_settings
from compendium.api.schemas import (
    ImportReportResponse,
    ImportRowErrorResponse,
)
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
from compendium.services.catalog import CatalogService
from compendium.services.import_export import (
    ExportFilters,
    ExportService,
    ImportMode,
    ImportOptions,
    ImportReport,
    ImportService,
)

import_router = APIRouter()
export_router = APIRouter()


def _make_importer(session: Session, actor: AppUser | None) -> ImportService:
    catalog = CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=None,
    )
    return ImportService(
        session=session,
        catalog=catalog,
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
    )


def _mode(value: str) -> ImportMode:
    try:
        return ImportMode(value)
    except ValueError as exc:
        valid = ", ".join(m.value for m in ImportMode)
        raise HTTPException(
            status_code=422,
            detail=f"Unknown mode '{value}'. Valid: {valid}",
        ) from exc


def _report_to_response(report: ImportReport) -> ImportReportResponse:
    return ImportReportResponse(
        source=report.source,
        filename=report.filename,
        total_rows=report.total_rows,
        created_works=report.created_works,
        added_copies=report.added_copies,
        skipped_duplicates=report.skipped_duplicates,
        enriched_rows=report.enriched_rows,
        errors=[
            ImportRowErrorResponse(
                row_number=e.row_number, identifier=e.identifier, message=e.message
            )
            for e in report.errors
        ],
        dry_run=report.dry_run,
    )


def _options(
    dry_run: bool,
    mode: str,
    default_branch: str | None,
    default_media_type: str | None,
    barcode_prefix: str | None,
    enrich: bool = False,
) -> ImportOptions:
    return ImportOptions(
        mode=_mode(mode),
        dry_run=dry_run,
        default_branch_code=default_branch,
        default_media_type=default_media_type,
        barcode_prefix=barcode_prefix,
        enrich_from_external=enrich,
    )


@import_router.post("/csv", response_model=ImportReportResponse)
async def import_csv(
    file: UploadFile = File(..., description="CSV file."),
    dry_run: bool = Query(False),
    mode: str = Query("append"),
    default_branch: str | None = Query(None),
    default_media_type: str | None = Query(None),
    barcode_prefix: str | None = Query(None),
    enrich: bool = Query(False, description="Fill missing fields from the external metadata source per row."),
    content_length: int | None = Header(default=None, alias="content-length"),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("catalog.import")),
) -> ImportReportResponse:
    import io as _io

    data = await read_upload_bounded(
        file, cap=settings.max_upload_bytes, content_length=content_length
    )
    try:
        text_stream = _io.StringIO(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=422, detail=f"CSV must be UTF-8 encoded: {exc}"
        ) from exc
    options = _options(dry_run, mode, default_branch, default_media_type, barcode_prefix, enrich)
    importer = _make_importer(session, user)
    try:
        report = importer.import_csv(text_stream, options, filename=file.filename)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _report_to_response(report)


@import_router.post("/marc", response_model=ImportReportResponse)
async def import_marc(
    file: UploadFile = File(..., description="MARC21 binary (.mrc) or MARCXML file."),
    dry_run: bool = Query(False),
    mode: str = Query("append"),
    default_branch: str | None = Query(None),
    default_media_type: str | None = Query(None),
    barcode_prefix: str | None = Query(None),
    is_xml: bool = Query(
        False,
        alias="xml",
        description="Set true if uploading MARCXML instead of binary MARC21.",
    ),
    enrich: bool = Query(False, description="Fill missing fields from the external metadata source per record."),
    content_length: int | None = Header(default=None, alias="content-length"),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("catalog.import")),
) -> ImportReportResponse:
    import io as _io

    data = await read_upload_bounded(
        file, cap=settings.max_upload_bytes, content_length=content_length
    )
    stream = _io.BytesIO(data)
    auto_xml = is_xml or (
        file.filename is not None
        and file.filename.lower().endswith((".xml", ".marcxml"))
    )
    options = _options(dry_run, mode, default_branch, default_media_type, barcode_prefix, enrich)
    importer = _make_importer(session, user)
    try:
        if auto_xml:
            report = importer.import_marcxml(stream, options, filename=file.filename)
        else:
            report = importer.import_marc(stream, options, filename=file.filename)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _report_to_response(report)


def _filters_from_query(
    media_type: str | None,
    branch: str | None,
    since: str | None,
) -> ExportFilters:
    parsed: datetime | None = None
    if since:
        try:
            parsed = datetime.fromisoformat(since)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"since must be ISO-8601 (YYYY-MM-DD), got '{since}'",
            ) from exc
    return ExportFilters(
        media_type_code=media_type, branch_code=branch, since=parsed
    )


@export_router.get("/csv")
def export_csv_endpoint(
    media_type: str | None = Query(None),
    branch: str | None = Query(None),
    since: str | None = Query(None),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("item.view")),
) -> StreamingResponse:
    import io as _io

    filters = _filters_from_query(media_type, branch, since)
    buf = _io.StringIO()
    ExportService(work_repo=SqlWorkRepository(session)).export_csv(buf, filters)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="compendium.csv"'},
    )


@export_router.get("/marc")
def export_marc_endpoint(
    media_type: str | None = Query(None),
    branch: str | None = Query(None),
    since: str | None = Query(None),
    xml: bool = Query(False),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("item.view")),
) -> StreamingResponse:
    import io as _io

    filters = _filters_from_query(media_type, branch, since)
    buf = _io.BytesIO()
    exporter = ExportService(work_repo=SqlWorkRepository(session))
    if xml:
        exporter.export_marcxml(buf, filters)
        media = "application/marcxml+xml"
        name = "compendium.xml"
    else:
        exporter.export_marc(buf, filters)
        media = "application/marc"
        name = "compendium.mrc"
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
