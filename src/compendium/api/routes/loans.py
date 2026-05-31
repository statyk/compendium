from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import CheckoutRequest, LoanResponse
from compendium.services.site_settings import get_site_setting
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.audit import AuditService
from compendium.services.calendar import CalendarService
from compendium.services.circulation import CirculationService
from compendium.api.deps import get_calendar_svc

router = APIRouter()


def _circulation(
    session: Session,
    actor: AppUser | None = None,
    calendar_svc: CalendarService | None = None,
) -> CirculationService:
    settings = get_settings()
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        hold_pickup_days=get_site_setting("hold_pickup_days"),
        calendar_svc=calendar_svc,
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        source="api",
        item_note_repo=SqlItemNoteRepository(session),
    )


@router.post("/checkout", status_code=201, response_model=LoanResponse)
def checkout(
    body: CheckoutRequest,
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_permission("loan.checkout")),
    calendar_svc: CalendarService = Depends(get_calendar_svc),
) -> LoanResponse:
    try:
        loan = _circulation(session, actor=user, calendar_svc=calendar_svc).checkout(
            body.barcode, body.card_number, override_holds=body.override_holds
        )
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LoanResponse.model_validate(loan)


@router.post("/{loan_id}/checkin", response_model=LoanResponse)
def checkin(
    loan_id: int = Path(),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("loan.checkin")),
) -> LoanResponse:
    try:
        loan = _circulation(session).checkin_by_id(loan_id)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LoanResponse.model_validate(loan)


@router.post("/{loan_id}/renew", response_model=LoanResponse)
def renew(
    loan_id: int = Path(),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("loan.renew.any")),
    calendar_svc: CalendarService = Depends(get_calendar_svc),
) -> LoanResponse:
    try:
        loan = _circulation(session, calendar_svc=calendar_svc).renew_by_id(loan_id)
    except (NotFoundError, BusinessRuleError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return LoanResponse.model_validate(loan)


@router.get("", response_model=list[LoanResponse])
def list_active_loans(
    due: str | None = Query(default=None),
    branch: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("loan.view.any")),
) -> list[LoanResponse]:
    """System-wide active loans, librarian view."""
    branch_id: int | None = None
    if branch:
        b = SqlBranchRepository(session).get_by_code(branch)
        if b is not None:
            branch_id = b.id
    loans = SqlLoanRepository(session).list_active(
        due=due, branch_id=branch_id, query=q, limit=limit, offset=offset
    )
    return [LoanResponse.model_validate(l) for l in loans]


@router.get("/patron/{card_number}", response_model=list[LoanResponse])
def list_patron_loans(
    card_number: str = Path(),
    status: str = Query(default="active"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("loan.view.any")),
) -> list[LoanResponse]:
    """Patron loan history (active / returned / all)."""
    patron = SqlPatronRepository(session).get_by_card_number(card_number)
    if patron is None:
        raise HTTPException(status_code=404, detail=f"No patron with card '{card_number}'")
    if status not in ("active", "returned", "all"):
        raise HTTPException(status_code=422, detail="status must be active|returned|all")
    loans = SqlLoanRepository(session).list_for_patron(
        patron.id, status=status, limit=limit, offset=offset
    )
    return [LoanResponse.model_validate(l) for l in loans]


@router.get("/item/{barcode}", response_model=list[LoanResponse])
def list_item_loans(
    barcode: str = Path(),
    limit: int = Query(default=25, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("loan.view.any")),
) -> list[LoanResponse]:
    """Loan history for a specific copy."""
    item = SqlItemRepository(session).get_by_barcode(barcode)
    if item is None:
        raise HTTPException(status_code=404, detail=f"No item with barcode '{barcode}'")
    loans = SqlLoanRepository(session).list_for_item(item.id, limit=limit, offset=offset)
    return [LoanResponse.model_validate(l) for l in loans]
