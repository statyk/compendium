from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from compendium.api.deps import require_permission
from compendium.api.schemas import ItemDetail
from compendium.db.session import get_session
from compendium.domain.models import AppUser
from compendium.repositories.sql.item_repository import SqlItemRepository

router = APIRouter()


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
