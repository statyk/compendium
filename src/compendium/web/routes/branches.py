from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "branch.edit"
_VALID_SCHEMES = {"lcc", "ddc", "none"}


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean, status_code=status_code)
    if fresh:
        set_csrf_cookie(resp, fresh)
    return resp


@router.get("/branches")
def branch_list(
    request: Request,
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    branches = SqlBranchRepository(session).list()
    return _render(
        "branches/list.html",
        request,
        {"request": request, "user": user, "branches": branches, "message": message, "error": error},
    )


@router.get("/branches/{branch_id}/edit")
def branch_edit_form(
    branch_id: int,
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    branch = SqlBranchRepository(session).get(branch_id)
    if branch is None:
        return _render("error.html", request, {"request": request, "user": user, "detail": "Branch not found"}, 404)
    return _render(
        "branches/edit.html",
        request,
        {"request": request, "user": user, "branch": branch, "error": None},
    )


@router.post("/branches/{branch_id}/edit")
def branch_edit(
    branch_id: int,
    request: Request,
    name: str = Form(default=""),
    default_classification_scheme: str = Form(default="none"),
    location_code: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    scheme = default_classification_scheme.lower()
    repo = SqlBranchRepository(session)
    branch = repo.get(branch_id)
    if branch is None:
        return _render("error.html", request, {"request": request, "user": user, "detail": "Branch not found"}, 404)
    new_name = name.strip()
    if not new_name or len(new_name) > 128:
        return _render(
            "branches/edit.html",
            request,
            {"request": request, "user": user, "branch": branch, "error": "Name is required (max 128 characters)."},
            422,
        )
    if scheme not in _VALID_SCHEMES:
        return _render(
            "branches/edit.html",
            request,
            {"request": request, "user": user, "branch": branch, "error": f"Invalid scheme '{scheme}'."},
            422,
        )
    loc = location_code.strip() or None
    if loc is not None and (len(loc) != 4 or not loc.isdigit()):
        return _render(
            "branches/edit.html",
            request,
            {"request": request, "user": user, "branch": branch, "error": "Location code must be exactly 4 decimal digits (e.g. 0001)."},
            422,
        )
    branch.name = new_name
    branch.default_classification_scheme = scheme
    branch.location_code = loc
    repo.update(branch)
    return RedirectResponse("/ui/branches?message=Branch+updated.", status_code=303)
