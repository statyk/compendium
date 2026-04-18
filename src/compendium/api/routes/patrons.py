import random

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import CreatePatronRequest, PatronResponse
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Patron
from compendium.repositories.sql.patron_repository import SqlPatronRepository

router = APIRouter()


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
