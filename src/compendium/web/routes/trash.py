from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.trash_repository import SqlTrashRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.trash import TrashService
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "work.delete"


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean, status_code=status_code)
    if fresh:
        set_csrf_cookie(resp, fresh)
    return resp


def _svc(session: Session, user: AppUser) -> TrashService:
    return TrashService(
        trash_repo=SqlTrashRepository(session),
        work_repo=SqlWorkRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=user,
        source="web",
    )


@router.get("/catalog/{work_id}/delete-confirm")
def delete_confirm_form(
    work_id: int,
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    work = SqlWorkRepository(session).get(work_id)
    if work is None:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"Work {work_id} not found"},
            status_code=404,
        )
    repo = SqlTrashRepository(session)
    ctx = {
        "request": request,
        "user": user,
        "work": work,
        "item_count": len(work.items),
        "active_loans": repo.count_active_loans(work_id),
        "outstanding_fines": repo.count_outstanding_fines(work_id),
    }
    return _render("catalog/delete_confirm.html", request, ctx)


@router.post("/catalog/{work_id}/delete")
def delete_work(
    work_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        summary = _svc(session, user).delete_work(work_id)
    except NotFoundError:
        return _render(
            "error.html",
            request,
            {"request": request, "user": user, "message": f"Work {work_id} not found"},
            status_code=404,
        )
    except BusinessRuleError as exc:
        return RedirectResponse(
            f"/ui/catalog/{work_id}?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(
        f"/ui/trash?message={quote(f'Moved to trash: {summary.label}')}",
        status_code=303,
    )


@router.get("/trash")
def trash_list(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    rows = _svc(session, user).list_deleted_works(limit=100)
    return _render(
        "trash/list.html",
        request,
        {"request": request, "user": user, "rows": rows, "message": message, "error": error},
    )


@router.post("/trash/{trash_id}/restore")
def restore_work(
    trash_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        work = _svc(session, user).restore_work(trash_id)
    except (NotFoundError, BusinessRuleError) as exc:
        return RedirectResponse(f"/ui/trash?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(
        f"/ui/catalog/{work.id}?message={quote('Work restored from trash.')}",
        status_code=303,
    )


@router.get("/trash/{trash_id}/purge-confirm")
def purge_confirm_form(
    trash_id: int,
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    row = SqlTrashRepository(session).get(trash_id)
    if row is None:
        return RedirectResponse("/ui/trash?error=Entry+not+found", status_code=303)
    return _render(
        "trash/purge_confirm.html",
        request,
        {"request": request, "user": user, "row": row},
    )


@router.post("/trash/{trash_id}/purge")
def purge_entry(
    trash_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    try:
        _svc(session, user).purge(trash_id=trash_id)
    except NotFoundError as exc:
        return RedirectResponse(f"/ui/trash?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(
        "/ui/trash?message=Entry+permanently+deleted.", status_code=303
    )
