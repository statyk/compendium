"""Web UI for circulation & overdue reports."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.reports import ReportsService
from compendium.web.csrf import ensure_csrf, set_csrf_cookie
from compendium.web.deps import require_web_permission
from compendium.web.jinja import templates

router = APIRouter()

_PERM = "report.view"


def _svc(session: Session) -> ReportsService:
    return ReportsService(
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        work_repo=SqlWorkRepository(session),
        branch_repo=SqlBranchRepository(session),
    )


def _render(name: str, request: Request, ctx: dict, status_code: int = 200):
    token, fresh = ensure_csrf(request)
    ctx_clean = {k: v for k, v in ctx.items() if k != "request"}
    ctx_clean["csrf_token"] = token
    resp = templates.TemplateResponse(request, name, ctx_clean, status_code=status_code)
    if fresh:
        set_csrf_cookie(resp, fresh, get_settings().jwt_secret_key)
    return resp


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD") from exc


def _chart_json(value) -> str:
    """JSON-encode for embedding inside an inline <script>.

    json.dumps() does not escape '</', so a string containing '</script>'
    would close the surrounding <script> tag and execute injected JS.
    """
    return json.dumps(value).replace("</", "<\\/")


def _csv_response(rows: list[dict], fieldnames: list[str], filename: str) -> Response:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _branches(session: Session):
    return SqlBranchRepository(session).list()


@router.get("/reports")
def reports_index(
    request: Request,
    user: AppUser = Depends(require_web_permission(_PERM)),
):
    return _render("reports/index.html", request, {"request": request, "user": user})


@router.get("/reports/checkouts")
def report_checkouts(
    request: Request,
    months: int = Query(12, ge=1, le=60),
    branch: str = "",
    format: str = "",
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    rows = _svc(session).checkouts_per_month(
        months=months, branch_code=branch or None
    )
    if format == "csv":
        return _csv_response(
            [{"month": r.month, "count": r.count} for r in rows],
            ["month", "count"],
            "checkouts-per-month.csv",
        )
    return _render(
        "reports/checkouts.html",
        request,
        {
            "request": request,
            "user": user,
            "rows": rows,
            "months": months,
            "branches": _branches(session),
            "selected_branch": branch,
            "chart_labels": _chart_json([r.month for r in rows]),
            "chart_values": _chart_json([r.count for r in rows]),
        },
    )


@router.get("/reports/popular")
def report_popular(
    request: Request,
    since: str = "",
    until: str = "",
    limit: int = Query(20, ge=1, le=200),
    branch: str = "",
    format: str = "",
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    # Default: last 3 months
    now = datetime.now(tz=timezone.utc)
    default_since = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    since_val = since or default_since
    since_dt = _parse_date(since_val)
    until_dt = _parse_date(until) if until else None
    rows = _svc(session).popular_works(
        since=since_dt, until=until_dt, limit=limit, branch_code=branch or None
    )
    if format == "csv":
        return _csv_response(
            [
                {
                    "work_id": r.work_id,
                    "title": r.title,
                    "media_type": r.media_type_code,
                    "checkout_count": r.checkout_count,
                }
                for r in rows
            ],
            ["work_id", "title", "media_type", "checkout_count"],
            "popular-works.csv",
        )
    return _render(
        "reports/popular.html",
        request,
        {
            "request": request,
            "user": user,
            "rows": rows,
            "since": since_val,
            "until": until,
            "limit": limit,
            "branches": _branches(session),
            "selected_branch": branch,
            "chart_labels": _chart_json([r.title for r in rows]),
            "chart_values": _chart_json([r.checkout_count for r in rows]),
        },
    )


@router.get("/reports/dormant")
def report_dormant(
    request: Request,
    not_since: str = "",
    limit: int = Query(100, ge=1, le=500),
    branch: str = "",
    format: str = "",
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    # Default cutoff: one year ago
    now = datetime.now(tz=timezone.utc)
    default_cutoff = (now - timedelta(days=365)).strftime("%Y-%m-%d")
    cutoff_val = not_since or default_cutoff
    cutoff = _parse_date(cutoff_val)
    rows = _svc(session).dormant_items(
        not_since=cutoff, limit=limit, branch_code=branch or None
    )
    if format == "csv":
        return _csv_response(
            [
                {
                    "barcode": r.barcode,
                    "title": r.title,
                    "media_type": r.media_type_code,
                    "branch": r.branch_code,
                    "last_checkout": r.last_checkout_at.strftime("%Y-%m-%d")
                    if r.last_checkout_at
                    else "",
                }
                for r in rows
            ],
            ["barcode", "title", "media_type", "branch", "last_checkout"],
            "dormant-items.csv",
        )
    return _render(
        "reports/dormant.html",
        request,
        {
            "request": request,
            "user": user,
            "rows": rows,
            "not_since": cutoff_val,
            "limit": limit,
            "branches": _branches(session),
            "selected_branch": branch,
        },
    )


@router.get("/reports/overdues")
def report_overdues(
    request: Request,
    branch: str = "",
    format: str = "",
    user: AppUser = Depends(require_web_permission(_PERM)),
    session: Session = Depends(get_session),
):
    rows = _svc(session).current_overdues(branch_code=branch or None)
    if format == "csv":
        return _csv_response(
            [
                {
                    "loan_id": r.loan_id,
                    "patron_card": r.patron_card,
                    "patron_name": r.patron_name,
                    "item_barcode": r.item_barcode,
                    "title": r.title,
                    "due_at": r.due_at.strftime("%Y-%m-%d"),
                    "days_overdue": r.days_overdue,
                }
                for r in rows
            ],
            [
                "loan_id",
                "patron_card",
                "patron_name",
                "item_barcode",
                "title",
                "due_at",
                "days_overdue",
            ],
            "current-overdues.csv",
        )
    return _render(
        "reports/overdues.html",
        request,
        {
            "request": request,
            "user": user,
            "rows": rows,
            "branches": _branches(session),
            "selected_branch": branch,
        },
    )
