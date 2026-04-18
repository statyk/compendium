import random

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import CreatePatronRequest, PatronResponse
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser, Patron
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.services.patrons import PatronService

router = APIRouter()


def _patron_service(session: Session) -> PatronService:
    return PatronService(
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        hold_repo=SqlHoldRepository(session),
    )


@router.post("", status_code=201, response_model=PatronResponse)
def create_patron(
    body: CreatePatronRequest,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("patron.manage")),
) -> PatronResponse:
    repo = SqlPatronRepository(session)
    for _ in range(10):
        card = f"{random.randint(0, 99_999_999):08d}"
        if repo.get_by_card_number(card) is None:
            break
    patron = Patron(
        library_card_number=card,
        full_name=body.full_name,
        contact_email=body.contact_email,
        contact_phone=body.contact_phone,
    )
    repo.add(patron)
    return PatronResponse.model_validate(patron)


@router.post("/{card_number}/deactivate", response_model=PatronResponse)
def deactivate_patron(
    card_number: str,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("patron.manage")),
) -> PatronResponse:
    try:
        patron = _patron_service(session).deactivate(card_number)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PatronResponse.model_validate(patron)
