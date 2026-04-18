from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import ItemDetail
from compendium.db.session import get_session
from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService

router = APIRouter()


def _catalog(session: Session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
    )


@router.get("/{barcode}", response_model=ItemDetail)
def get_item(
    barcode: str,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("item.view")),
) -> ItemDetail:
    item = SqlItemRepository(session).get_by_barcode(barcode)
    if item is None:
        raise HTTPException(status_code=404, detail=f"No item with barcode '{barcode}'")
    return ItemDetail.model_validate(item)


@router.post("/{barcode}/withdraw", response_model=ItemDetail)
def withdraw_item(
    barcode: str,
    session: Session = Depends(get_session),
    _user: AppUser = Depends(require_permission("item.delete")),
) -> ItemDetail:
    try:
        item = _catalog(session).withdraw_item(barcode)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BusinessRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ItemDetail.model_validate(item)
