"""Remote phone-scanner pairing routes (``/ui/scan/*``).

A staff member generates a pairing QR at the desk; a phone scans it, claims a
short-lived session, and then streams barcode scans back to drive checkout,
checkin, or catalog-add — all attributed to the staff user who created the
pairing. The phone never authenticates as a user; it holds only a rotated
session secret (see ``require_scan_pairing``).

Security model:
- Only the SHA-256 hex of the *current* secret is stored (``token_hash``).
- The claim secret is single-use: claiming rotates ``token_hash`` to a fresh
  session secret, so replaying the claim URL finds no row → rejected.
- The phone's session cookie is distinct from the staff auth cookie.
- Every dispatch re-verifies the staff user still holds the mode's permission
  (defense in depth — perms can change after pairing).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import (
    BlockedByFinesError,
    BusinessRuleError,
    ExternalLookupError,
    HoldQueueBlockError,
    NotFoundError,
    ValidationError,
)
from compendium.domain.identifiers import ITEM_TYPE, PATRON_TYPE, validate_barcode
from compendium.domain.models import AppUser, ScanEvent, ScanPairing, ScanPendingItem
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.counters import SqlCounterRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.failed_login_repository import SqlFailedLoginRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.scan_event_repository import SqlScanEventRepository
from compendium.repositories.sql.scan_pairing_repository import SqlScanPairingRepository
from compendium.repositories.sql.scan_pending_item_repository import (
    SqlScanPendingItemRepository,
)
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.auth import has_permission
from compendium.services.calendar import CalendarService
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
from compendium.services.fines import FineService
from compendium.services.metadata import normalize_isbn
from compendium.services.rate_limit import RateLimitService
from compendium.services.site_settings import get_site_setting
from compendium.web.base_url import InsecureContextError, resolve_public_base_url
from compendium.web.csrf import check_csrf_form, ensure_csrf, set_csrf_cookie
from compendium.web.deps import (
    get_calendar_svc,
    require_scan_pairing,
    require_web_user,
    set_scan_cookie,
)
from compendium.web.jinja import templates
from compendium.web.qrcode import qr_svg

router = APIRouter()

# mode → permission required to use it
MODE_PERMISSION = {
    "checkout": "loan.checkout",
    "checkin": "loan.checkin",
    "catalog": "catalog.import",
}

_CLAIM_TTL = timedelta(minutes=2)  # short pre-claim TTL; session TTL set at claim


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


# ── service wiring (mirrors kiosk.py / catalog.py) ────────────────────────────


def _fine_svc(
    session: Session, calendar_svc: CalendarService | None = None
) -> FineService:
    return FineService(
        fine_repo=SqlFineRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        settings=get_settings(),
        calendar_svc=calendar_svc,
        source="scan",
    )


def _circ(
    session: Session, actor: AppUser, calendar_svc: CalendarService | None = None
) -> CirculationService:
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        hold_pickup_days=get_site_setting("hold_pickup_days"),
        fine_svc=_fine_svc(session, calendar_svc),
        calendar_svc=calendar_svc,
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="scan",
        item_note_repo=SqlItemNoteRepository(session),
    )


def _catalog_svc(session: Session, actor: AppUser) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="scan",
        hold_repo=SqlHoldRepository(session),
        counter_repo=SqlCounterRepository(session),
        item_note_repo=SqlItemNoteRepository(session),
    )


# ── idempotency guard (per-process, best-effort) ──────────────────────────────
#
# The phone JS already burst-collapses rapid identical scans; this is a server
# backstop against double-submits. Keyed by pairing id → (mode, code, ts). Not
# durable across processes/restarts — intentionally a best-effort backstop only.

_IDEMPOTENCY_WINDOW = 2.0  # seconds


class IdempotencyGuard:
    def __init__(self, *, window: float = _IDEMPOTENCY_WINDOW, clock=time.monotonic):
        self._window = window
        self._clock = clock
        self._last: dict[int, tuple[str, str, float]] = {}

    def is_duplicate(self, pairing_id: int, mode: str, code: str) -> bool:
        now = self._clock()
        prev = self._last.get(pairing_id)
        self._last[pairing_id] = (mode, code, now)
        # Bound growth: evict entries older than twice the window. Iterate over a
        # snapshot so a concurrent insert can't raise during the prune (a missed
        # eviction is benign — best-effort, per-process).
        cutoff = now - 2 * self._window
        for pid, (_m, _c, ts) in list(self._last.items()):
            if ts < cutoff:
                self._last.pop(pid, None)
        if prev is None:
            return False
        p_mode, p_code, p_ts = prev
        return (
            p_mode == mode
            and p_code == code
            and (now - p_ts) <= self._window
        )


_guard = IdempotencyGuard()


# ── state machine (pure-ish; injectable services for unit tests) ──────────────

_DIGITS_RE = re.compile(r"[\s\-]")


def _looks_like_isbn(code: str) -> bool:
    digits = _DIGITS_RE.sub("", code)
    return digits.isdigit() and len(digits) in (10, 13)


def run_state_machine(
    row: ScanPairing,
    code: str,
    *,
    patron_repo,
    checkout,
    checkin,
    add_from_isbn,
    fetch_metadata=None,
    queue_pending=None,
) -> dict:
    """Advance a scan session by one scanned ``code``.

    Mutates ``row`` (mode/borrower/count) in place and returns the JSON reply
    dict ``{ok, kind, message, mode, borrower, count, item_id, patron_id}``
    (``item_id``/``patron_id`` are ``None`` unless the action touched a specific
    item/patron). ``kind`` is one of ``borrower_set``, ``checkout``, ``checkin``,
    ``ignored``, ``error``, ``catalog_added``, or ``catalog_queued`` (the last
    when catalog review is on and the scan is parked for later approval).

    Service interactions are injected as callables so this is unit-testable with
    mocks:

    - ``checkout(barcode, card_number)`` → loan (with ``.item.work.title``)
    - ``checkin(barcode)`` → loan
    - ``add_from_isbn(code)`` → (work, item)
    - ``patron_repo.get_by_card_number(card)`` → Patron | None
    - ``fetch_metadata(code)`` → metadata dict | None (catalog mode)
    - ``queue_pending(code, meta)`` → None; parks a pending item for review
      (catalog mode with review enabled)
    """
    code = code.strip()
    parsed = validate_barcode(code)
    mode = row.mode

    if mode == "checkout":
        if parsed is not None and parsed.type == PATRON_TYPE:
            patron = patron_repo.get_by_card_number(code)
            if patron is None:
                return _reply(row, False, "error", "Card not recognized.")
            row.borrower_patron_id = patron.id
            row.count = 0
            name = patron.full_name or code
            return _reply(
                row, True, "borrower_set", f"Borrower: {name}", patron_id=patron.id
            )
        if parsed is not None and parsed.type == ITEM_TYPE:
            if row.borrower_patron_id is None:
                return _reply(row, False, "error", "Scan a patron card first")
            card = row.borrower.library_card_number
            try:
                loan = checkout(code, card)
            except Exception as exc:  # noqa: BLE001 — translated below
                return _circ_error(row, exc)
            row.count += 1
            title = _loan_title(loan)
            return _reply(
                row,
                True,
                "checkout",
                f"Checked out: {title}",
                item_id=(loan.item.id if loan and loan.item else None),
                patron_id=row.borrower_patron_id,
            )
        return _reply(row, False, "error", "Scan a patron card or item barcode")

    if mode == "checkin":
        if parsed is not None and parsed.type == ITEM_TYPE:
            try:
                loan = checkin(code)
            except Exception as exc:  # noqa: BLE001 — translated below
                return _circ_error(row, exc)
            row.count += 1
            title = _loan_title(loan)
            return _reply(
                row,
                True,
                "checkin",
                f"Checked in: {title}",
                item_id=(loan.item.id if loan and loan.item else None),
            )
        return _reply(row, False, "error", "Scan an item barcode")

    # catalog
    if parsed is not None:
        return _reply(row, False, "error", "That's not a catalog identifier")
    if _looks_like_isbn(code):
        if row.catalog_review:
            try:
                meta = fetch_metadata(code)
            except (
                BusinessRuleError,
                NotFoundError,
                ExternalLookupError,
                ValidationError,
            ) as exc:
                return _reply(row, False, "error", str(exc))
            queue_pending(code, meta)
            row.count += 1
            title = meta.get("title") or "title"
            return _reply(row, True, "catalog_queued", f"Queued for review: {title}")
        try:
            work, _item = add_from_isbn(code)
        except (
            BusinessRuleError,
            NotFoundError,
            ExternalLookupError,
            ValidationError,
        ) as exc:
            return _reply(row, False, "error", str(exc))
        row.count += 1
        title = work.title if work and work.title else "title"
        return _reply(row, True, "catalog_added", f"Added: {title}")
    return _reply(row, False, "error", "Add this title at the desk")


def _loan_title(loan) -> str:
    if loan and loan.item and loan.item.work and loan.item.work.title:
        return loan.item.work.title
    return "Item"


def _circ_error(row: ScanPairing, exc: Exception) -> dict:
    if isinstance(exc, NotFoundError):
        return _reply(row, False, "error", "Item not found.")
    if isinstance(exc, BlockedByFinesError):
        return _reply(
            row, False, "error", "Patron has outstanding fees. See the desk."
        )
    if isinstance(exc, HoldQueueBlockError):
        return _reply(
            row, False, "error", "Reserved for another patron. See the desk."
        )
    if isinstance(exc, (BusinessRuleError, ValidationError)):
        return _reply(row, False, "error", str(exc))
    # Re-raise anything we don't recognize so it surfaces rather than masking a bug.
    raise exc


def _reply(
    row: ScanPairing,
    ok: bool,
    kind: str,
    message: str,
    *,
    item_id: int | None = None,
    patron_id: int | None = None,
) -> dict:
    borrower = row.borrower.library_card_number if row.borrower else None
    return {
        "ok": ok,
        "kind": kind,
        "message": message,
        "mode": row.mode,
        "borrower": borrower,
        "count": row.count,
        "item_id": item_id,
        "patron_id": patron_id,
    }


# ── routes ────────────────────────────────────────────────────────────────────


def _qr_partial_html(
    request: Request, pairing: ScanPairing, claim_url: str
) -> HTMLResponse:
    svg = qr_svg(claim_url)
    csrf, fresh = ensure_csrf(request)
    html = templates.get_template("scan/_qr_partial.html").render(
        request=request,
        pairing=pairing,
        qr_svg=svg,
        csrf_token=csrf,
        events=[],
        pending=[],
    )
    resp = HTMLResponse(html)
    if fresh:
        set_csrf_cookie(resp, fresh)
    return resp


def _render_activity(
    request: Request, session: Session, pairing: ScanPairing, user: AppUser
) -> HTMLResponse:
    events = SqlScanEventRepository(session).recent_for_pairing(pairing.id, limit=25)
    pending = SqlScanPendingItemRepository(session).pending_for_user(user.id)
    csrf, fresh = ensure_csrf(request)
    html = templates.get_template("scan/_activity_partial.html").render(
        request=request, pairing=pairing, events=events,
        pending=pending, csrf_token=csrf,
    )
    resp = HTMLResponse(html)
    if fresh:
        set_csrf_cookie(resp, fresh)
    return resp


@router.post("/scan/pairings")
def create_pairing(
    request: Request,
    checkout: str = Form(default=""),
    checkin: str = Form(default=""),
    catalog: str = Form(default=""),
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_user),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    perms = user.role.permissions
    # Gate: must hold at least one of the three mode permissions.
    if not any(has_permission(perms, p) for p in MODE_PERMISSION.values()):
        return HTMLResponse(
            '<p class="error-banner">You don\'t have permission to pair a '
            "phone scanner.</p>",
            status_code=403,
        )
    requested = [
        m
        for m, flag in (("checkout", checkout), ("checkin", checkin), ("catalog", catalog))
        if flag.strip().lower() in ("1", "true", "on", "yes")
    ]
    allowed_modes = [
        m for m in requested if has_permission(perms, MODE_PERMISSION[m])
    ]
    if not allowed_modes:
        return HTMLResponse(
            '<p class="error-banner">Select at least one mode you have '
            "permission to use.</p>",
            status_code=400,
        )

    try:
        base = resolve_public_base_url(request)
    except InsecureContextError as exc:
        return HTMLResponse(
            f'<p class="warning-banner">Phone pairing needs HTTPS. {escape(str(exc))}</p>',
            status_code=400,
        )

    claim = secrets.token_urlsafe(32)
    now = _now()
    pairing = ScanPairing(
        token_hash=_hash(claim),
        user_id=user.id,
        allowed_modes=allowed_modes,
        mode=allowed_modes[0],
        count=0,
        created_at=now,
        expires_at=now + _CLAIM_TTL,
        claimed_at=None,
        revoked_at=None,
    )
    SqlScanPairingRepository(session).add(pairing)

    claim_url = f"{base}/ui/scan/pair?c={claim}"
    return _qr_partial_html(request, pairing, claim_url)


@router.get("/scan/pairings/{pairing_id}/log", response_class=HTMLResponse)
def pairing_log(
    pairing_id: int,
    request: Request,
    user: AppUser = Depends(require_web_user),
    session: Session = Depends(get_session),
):
    pairing = SqlScanPairingRepository(session).get(pairing_id)
    if pairing is None or pairing.user_id != user.id:
        return HTMLResponse(
            '<p class="error-banner">Pairing not found.</p>', status_code=404
        )
    return _render_activity(request, session, pairing, user)


@router.get("/scan/pair")
def claim_pairing(
    request: Request,
    c: str = "",
    session: Session = Depends(get_session),
):
    claim = c.strip()
    if not claim:
        return _claim_error(request, "This pairing link is invalid.")
    rl = RateLimitService(SqlFailedLoginRepository(session))
    retry_after = rl.check("scan_claim", claim)
    if retry_after is not None:
        return _claim_error(
            request,
            f"Too many attempts. Try again in {retry_after} seconds.",
        )
    repo = SqlScanPairingRepository(session)
    row = repo.get_by_token_hash(_hash(claim))
    # Accepted TOCTOU: two concurrent claims could both pass this check before
    # either flushes, yielding two sessions. Exploitability is low (single-scan
    # QR, 2-min pre-claim TTL, 256-bit secret) and SQLite serializes writes. A
    # Postgres ``SELECT ... FOR UPDATE`` on the row would close it if ever needed.
    if (
        row is None
        or row.claimed_at is not None
        or row.revoked_at is not None
        or row.expires_at < _now()
    ):
        rl.record_failure("scan_claim", claim)
        return _claim_error(
            request, "This pairing link has expired or was already used."
        )
    rl.clear("scan_claim", claim)

    # Rotate to a single-use session secret.
    session_secret = secrets.token_urlsafe(32)
    row.token_hash = _hash(session_secret)
    row.claimed_at = _now()
    row.expires_at = _now() + timedelta(
        minutes=int(get_site_setting("scan_session_minutes"))
    )
    session.flush()

    csrf, fresh = ensure_csrf(request)
    resp = templates.TemplateResponse(
        request,
        "scan/phone.html",
        {
            "request": request,
            "pairing": row,
            "allowed_modes": row.allowed_modes,
            "mode": row.mode,
            "csrf_token": csrf,
        },
    )
    if fresh:
        set_csrf_cookie(resp, fresh)
    set_scan_cookie(resp, session_secret)
    return resp


def _claim_error(request: Request, message: str):
    return templates.TemplateResponse(
        request,
        "scan/claim_error.html",
        {"request": request, "message": message},
        status_code=403,
    )


@router.post("/scan/dispatch")
def dispatch(
    request: Request,
    code: str = Form(default=""),
    csrf_token: str = Form(default=""),
    row: ScanPairing = Depends(require_scan_pairing),
    session: Session = Depends(get_session),
    calendar_svc: CalendarService = Depends(get_calendar_svc),
):
    check_csrf_form(request, csrf_token)
    code = code.strip()
    if not code:
        return JSONResponse(_reply(row, False, "error", "Empty scan."))

    actor = row.user
    # Defense in depth: the staff user can be deactivated or lose perms after
    # pairing. require_web_user rejects inactive users; the scan path must too,
    # else a deactivated staffer keeps a working paired phone until session TTL.
    if not actor.is_active:
        return JSONResponse(
            _reply(row, False, "error", "Session ended."),
            status_code=403,
        )
    if not has_permission(actor.role.permissions, MODE_PERMISSION[row.mode]):
        return JSONResponse(
            _reply(row, False, "error", "Permission revoked for this mode."),
            status_code=403,
        )

    if _guard.is_duplicate(row.id, row.mode, code):
        return JSONResponse(_reply(row, True, "ignored", "Duplicate scan ignored."))

    circ = _circ(session, actor, calendar_svc)
    cat = _catalog_svc(session, actor)

    def _queue_pending(code: str, meta: dict) -> None:
        pending = ScanPendingItem(
            pairing_id=row.id,
            isbn=normalize_isbn(code) or code,
            title=(meta.get("title") or "Untitled")[:512],
            meta_json=meta,
            cover_url=meta.get("cover_image_url"),
            status="pending",
        )
        SqlScanPendingItemRepository(session).add(pending)

    reply = run_state_machine(
        row,
        code,
        patron_repo=SqlPatronRepository(session),
        checkout=circ.checkout,
        checkin=circ.checkin,
        add_from_isbn=cat.add_from_isbn,
        fetch_metadata=cat.fetch_book_metadata,
        queue_pending=_queue_pending,
    )
    session.flush()

    if reply["kind"] != "ignored":
        SqlScanEventRepository(session).add(
            ScanEvent(
                pairing_id=row.id,
                mode=row.mode,
                kind="ok" if reply["ok"] else "error",
                message=reply["message"][:255],
                item_id=reply.get("item_id"),
                patron_id=reply.get("patron_id"),
            )
        )
        session.flush()

    return JSONResponse(reply)


@router.post("/scan/mode")
def switch_mode(
    request: Request,
    mode: str = Form(default=""),
    csrf_token: str = Form(default=""),
    row: ScanPairing = Depends(require_scan_pairing),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    # A deactivated staff user must not keep acting through a paired phone.
    if not row.user.is_active:
        return JSONResponse(
            _reply(row, False, "error", "Session ended."),
            status_code=403,
        )
    mode = mode.strip()
    if mode not in row.allowed_modes:
        return JSONResponse(
            _reply(row, False, "error", "Mode not allowed for this session."),
            status_code=400,
        )
    if row.mode == "checkout" and mode != "checkout":
        row.borrower_patron_id = None
        row.count = 0
    row.mode = mode
    session.flush()
    return JSONResponse(_reply(row, True, "mode_set", f"Mode: {mode}"))


@router.post("/scan/review")
def set_review(
    request: Request,
    enabled: str = Form(default=""),
    csrf_token: str = Form(default=""),
    row: ScanPairing = Depends(require_scan_pairing),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    if not row.user.is_active:
        return JSONResponse(
            _reply(row, False, "error", "Session ended."), status_code=403
        )
    if "catalog" not in row.allowed_modes:
        return JSONResponse(
            _reply(row, False, "error", "Catalog mode not enabled for this session."),
            status_code=400,
        )
    row.catalog_review = enabled.strip().lower() in ("1", "true", "on", "yes")
    session.flush()
    msg = "Review first: on" if row.catalog_review else "Review first: off"
    reply = _reply(row, True, "review_set", msg)
    reply["catalog_review"] = row.catalog_review
    return JSONResponse(reply)


@router.post("/scan/pairings/{pairing_id}/unpair", response_class=HTMLResponse)
def unpair(
    pairing_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_user),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    pairing = SqlScanPairingRepository(session).get(pairing_id)
    if pairing is None or pairing.user_id != user.id:
        return HTMLResponse(
            '<p class="error-banner">Pairing not found.</p>', status_code=404
        )
    pairing.revoked_at = _now()
    session.flush()
    return HTMLResponse('<p class="success-banner">Phone unpaired.</p>')


def _owned_pairing_or_404(
    session: Session, pairing_id: int, user: AppUser
) -> ScanPairing | None:
    pairing = SqlScanPairingRepository(session).get(pairing_id)
    if pairing is None or pairing.user_id != user.id:
        return None
    return pairing


@router.post(
    "/scan/pairings/{pairing_id}/pending/{pending_id}/approve",
    response_class=HTMLResponse,
)
def approve_pending(
    pairing_id: int,
    pending_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_user),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    if not has_permission(user.role.permissions, MODE_PERMISSION["catalog"]):
        return HTMLResponse(
            '<p class="error-banner">You don\'t have permission to catalog items.</p>',
            status_code=403,
        )
    pairing = _owned_pairing_or_404(session, pairing_id, user)
    if pairing is None:
        return HTMLResponse(
            '<p class="error-banner">Pairing not found.</p>', status_code=404
        )
    pend_repo = SqlScanPendingItemRepository(session)
    pend = pend_repo.get(pending_id)
    if pend is None or pend.pairing_id != pairing_id or pend.status != "pending":
        return HTMLResponse(
            '<p class="error-banner">Item already resolved.</p>', status_code=404
        )
    try:
        _work, item = _catalog_svc(session, user).add_from_metadata(
            pend.meta_json, media_type_code="book"
        )
    except (BusinessRuleError, NotFoundError, ValidationError) as exc:
        return HTMLResponse(
            f'<p class="error-banner">{escape(str(exc))}</p>', status_code=400
        )
    pend.status = "approved"
    pend.resolved_at = _now()
    pend.resolved_by = user.id
    pend.created_item_id = item.id
    SqlScanEventRepository(session).add(
        ScanEvent(
            pairing_id=pairing_id, mode="catalog", kind="ok",
            message=f"Catalogued: {pend.title}"[:255], item_id=item.id,
        )
    )
    session.flush()
    return _render_activity(request, session, pairing, user)


@router.post(
    "/scan/pairings/{pairing_id}/pending/{pending_id}/discard",
    response_class=HTMLResponse,
)
def discard_pending(
    pairing_id: int,
    pending_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    user: AppUser = Depends(require_web_user),
    session: Session = Depends(get_session),
):
    check_csrf_form(request, csrf_token)
    if not has_permission(user.role.permissions, MODE_PERMISSION["catalog"]):
        return HTMLResponse(
            '<p class="error-banner">You don\'t have permission to catalog items.</p>',
            status_code=403,
        )
    pairing = _owned_pairing_or_404(session, pairing_id, user)
    if pairing is None:
        return HTMLResponse(
            '<p class="error-banner">Pairing not found.</p>', status_code=404
        )
    pend_repo = SqlScanPendingItemRepository(session)
    pend = pend_repo.get(pending_id)
    if pend is not None and pend.pairing_id == pairing_id and pend.status == "pending":
        pend.status = "discarded"
        pend.resolved_at = _now()
        pend.resolved_by = user.id
        session.flush()
    return _render_activity(request, session, pairing, user)
