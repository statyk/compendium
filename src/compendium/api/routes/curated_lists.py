from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import (
    AddWorkToCuratedListRequest,
    CreateCuratedListRequest,
    CuratedListEntryResponse,
    CuratedListResponse,
    CuratedListSummaryResponse,
    ReorderCuratedListRequest,
    SetAnnotationRequest,
    UpdateCuratedListRequest,
)
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser, CuratedList, CuratedListEntry
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.curated_list_repository import SqlCuratedListRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.curated_lists import CuratedListService, _MISSING

router = APIRouter()

_PERM = "curatedlist.manage"


def _svc(session: Session, actor: AppUser) -> CuratedListService:
    return CuratedListService(
        curated_list_repo=SqlCuratedListRepository(session),
        work_repo=SqlWorkRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
    )


def _build_entry_response(entry: CuratedListEntry) -> CuratedListEntryResponse:
    return CuratedListEntryResponse(
        list_id=entry.list_id,
        work_id=entry.work_id,
        display_order=entry.display_order,
        annotation=entry.annotation,
        work_title=entry.work.title if entry.work else None,
    )


def _build_response(cl: CuratedList) -> CuratedListResponse:
    return CuratedListResponse(
        id=cl.id,
        slug=cl.slug,
        name=cl.name,
        description=cl.description,
        is_public=cl.is_public,
        is_featured=cl.is_featured,
        display_order=cl.display_order,
        created_at=cl.created_at,
        updated_at=cl.updated_at,
        entries=[_build_entry_response(e) for e in cl.entries],
    )


def _build_summary(cl: CuratedList) -> CuratedListSummaryResponse:
    return CuratedListSummaryResponse(
        id=cl.id,
        slug=cl.slug,
        name=cl.name,
        description=cl.description,
        is_public=cl.is_public,
        is_featured=cl.is_featured,
        display_order=cl.display_order,
        work_count=len(cl.entries),
    )


@router.post("", status_code=201, response_model=CuratedListResponse)
def create_curated_list(
    body: CreateCuratedListRequest,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    try:
        cl = _svc(session, actor).create(
            name=body.name,
            description=body.description,
            is_public=body.is_public,
            is_featured=body.is_featured,
            display_order=body.display_order,
        )
        return _build_response(cl)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("", response_model=list[CuratedListSummaryResponse])
def list_curated_lists(
    limit: int = 50,
    offset: int = 0,
    public_only: bool = False,
    featured_only: bool = False,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    lists = _svc(session, actor).list(
        limit=limit,
        offset=offset,
        public_only=public_only,
        featured_only=featured_only,
    )
    return [_build_summary(cl) for cl in lists]


@router.get("/{slug}", response_model=CuratedListResponse)
def get_curated_list(
    slug: str,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    try:
        cl = _svc(session, actor).get_by_slug(slug)
        return _build_response(cl)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{slug}", response_model=CuratedListResponse)
def update_curated_list(
    slug: str,
    body: UpdateCuratedListRequest,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    svc = _svc(session, actor)
    try:
        cl = svc.get_by_slug(slug)
        updated = svc.update(
            cl.id,
            name=body.name if body.name is not None else _MISSING,
            description=body.description if body.description is not None else _MISSING,
            is_public=body.is_public if body.is_public is not None else _MISSING,
            is_featured=body.is_featured if body.is_featured is not None else _MISSING,
            display_order=body.display_order if body.display_order is not None else _MISSING,
            slug=body.slug if body.slug is not None else _MISSING,
        )
        return _build_response(updated)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.delete("/{slug}", status_code=204)
def delete_curated_list(
    slug: str,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    svc = _svc(session, actor)
    try:
        cl = svc.get_by_slug(slug)
        svc.delete(cl.id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BusinessRuleError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{slug}/works", status_code=201, response_model=CuratedListEntryResponse)
def add_work_to_list(
    slug: str,
    body: AddWorkToCuratedListRequest,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    svc = _svc(session, actor)
    try:
        cl = svc.get_by_slug(slug)
        entry = svc.add_work(cl.id, body.work_id, body.annotation)
        return _build_entry_response(entry)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BusinessRuleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.delete("/{slug}/works/{work_id}", status_code=204)
def remove_work_from_list(
    slug: str,
    work_id: int,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    svc = _svc(session, actor)
    try:
        cl = svc.get_by_slug(slug)
        svc.remove_work(cl.id, work_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BusinessRuleError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.patch("/{slug}/works/{work_id}/annotation", response_model=CuratedListEntryResponse)
def set_work_annotation(
    slug: str,
    work_id: int,
    body: SetAnnotationRequest,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    svc = _svc(session, actor)
    try:
        cl = svc.get_by_slug(slug)
        entry = svc.set_annotation(cl.id, work_id, body.annotation)
        return _build_entry_response(entry)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{slug}/reorder", response_model=CuratedListResponse)
def reorder_list(
    slug: str,
    body: ReorderCuratedListRequest,
    session: Session = Depends(get_session),
    actor: AppUser = Depends(require_permission(_PERM)),
):
    svc = _svc(session, actor)
    try:
        cl = svc.get_by_slug(slug)
        updated = svc.reorder(cl.id, body.work_ids)
        return _build_response(updated)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except BusinessRuleError as e:
        raise HTTPException(status_code=422, detail=str(e))
