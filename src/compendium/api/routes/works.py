from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from compendium.api.deps import get_optional_user
from compendium.api.schemas import WorkSummary
from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser
from compendium.repositories.sql.work_repository import SqlWorkRepository

router = APIRouter()


@router.get("/search", response_model=list[WorkSummary])
def search_works(
    q: str = Query(min_length=1),
    session: Session = Depends(get_session),
    user: AppUser | None = Depends(get_optional_user),
) -> list[WorkSummary]:
    settings = get_settings()
    if not settings.guest_search_enabled and user is None:
        raise HTTPException(status_code=401, detail="Authentication required to search")
    works = SqlWorkRepository(session).search(q)
    return [WorkSummary.model_validate(w) for w in works]
