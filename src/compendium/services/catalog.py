from compendium.domain.enums import ItemStatus
from compendium.domain.errors import BusinessRuleError, ExternalLookupError, NotFoundError
from compendium.domain.models import AppUser, Creator, Item, Work, WorkCreator
from compendium.repositories.base import (
    BranchRepository,
    CreatorRepository,
    ItemRepository,
    MediaTypeRepository,
    WorkRepository,
)
from compendium.services.audit import AuditAction, AuditEntityType, AuditService
from compendium.services.metadata import lookup_isbn, normalize_isbn, parse_open_library


class CatalogService:
    def __init__(
        self,
        work_repo: WorkRepository,
        item_repo: ItemRepository,
        creator_repo: CreatorRepository,
        branch_repo: BranchRepository,
        media_type_repo: MediaTypeRepository,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._works = work_repo
        self._items = item_repo
        self._creators = creator_repo
        self._branches = branch_repo
        self._media_types = media_type_repo
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_from_isbn(
        self,
        raw_isbn: str,
        location: str | None = None,
    ) -> tuple[Work, Item]:
        """Look up an ISBN on Open Library, create Work + Item, return both.

        If a Work with this ISBN already exists, a new Item (copy) is added
        to that Work rather than creating a duplicate Work record.
        """
        isbn = normalize_isbn(raw_isbn)

        work = self._works.get_by_isbn(isbn)
        new_work = work is None
        if work is None:
            data = lookup_isbn(isbn)
            if not data:
                raise ExternalLookupError(
                    f"ISBN {isbn} was not found in Open Library. "
                    "Use 'item add-manual' to enter metadata by hand."
                )
            meta = parse_open_library(data, isbn)
            work = self._create_work(meta)

        item = self._create_item(work, location=location)
        if new_work:
            self._record(
                AuditEntityType.WORK, work.id, AuditAction.CREATE,
                {"snapshot": {"title": work.title, "isbn": work.isbn}},
            )
        self._record(
            AuditEntityType.ITEM, item.id, AuditAction.CREATE,
            {"snapshot": {"barcode": item.barcode, "work_id": work.id}},
        )
        return work, item

    def withdraw_item(self, barcode: str) -> Item:
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")
        blocked = {ItemStatus.CHECKED_OUT.value, ItemStatus.ON_HOLD.value}
        if item.status in blocked:
            raise BusinessRuleError(f"Item '{barcode}' cannot be withdrawn while {item.status}")
        item.status = ItemStatus.WITHDRAWN.value
        result = self._items.update(item)
        self._record(
            AuditEntityType.ITEM, item.id, AuditAction.WITHDRAW,
            {"snapshot": {"barcode": item.barcode, "work_id": item.work_id}},
        )
        return result

    def add_item_to_work(self, work_id: int, location: str | None = None) -> Item:
        """Add another physical copy of an existing Work."""
        work = self._works.get(work_id)
        if work is None:
            raise NotFoundError(f"No Work with id={work_id}")
        item = self._create_item(work, location=location)
        self._record(
            AuditEntityType.ITEM, item.id, AuditAction.CREATE,
            {"snapshot": {"barcode": item.barcode, "work_id": work.id}},
        )
        return item

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _create_work(self, meta: dict) -> Work:
        book_mt = self._media_types.get_by_code("book")

        work = Work(
            title=meta["title"],
            subtitle=meta.get("subtitle"),
            media_type_id=book_mt.id,
            publisher=meta.get("publisher"),
            publication_year=meta.get("publication_year"),
            language="en",
            description=meta.get("description"),
            isbn=meta["isbn"],
            cover_image_url=meta.get("cover_image_url"),
            external_ids=meta.get("external_ids", {}),
        )
        self._works.add(work)

        for order, name in enumerate(meta.get("authors", [])):
            creator = self._get_or_create_creator(name)
            # Append to collection — back_populates sets wc.work automatically.
            # Do NOT also pass work= to the constructor; that would add it twice.
            work.creators.append(WorkCreator(creator=creator, role="author", display_order=order))

        return work

    def _get_or_create_creator(self, display_name: str) -> Creator:
        sort_name = _to_sort_name(display_name)
        creator = self._creators.get_by_sort_name(sort_name)
        if creator is None:
            creator = Creator(display_name=display_name, sort_name=sort_name)
            self._creators.add(creator)
        return creator

    def _create_item(self, work: Work, location: str | None = None) -> Item:
        branch = self._branches.get_default()
        accession = self._next_accession()
        item = Item(
            work_id=work.id,
            branch_id=branch.id,  # type: ignore[union-attr]
            barcode=accession,
            accession_number=accession,
            location=location,
        )
        return self._items.add(item)

    def _next_accession(self) -> str:
        n = self._items.count_all() + 1
        return f"{n:06d}"

    def _record(
        self,
        entity_type: str,
        entity_id: int | None,
        action: str,
        details: dict | None = None,
    ) -> None:
        if self._audit is not None:
            self._audit.record(
                actor=self._actor,
                actor_label=self._actor_label,
                source=self._source,
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                details=details,
            )


def _to_sort_name(display_name: str) -> str:
    """'Frank Herbert' → 'Herbert, Frank'  (simple last-word heuristic)."""
    parts = display_name.strip().split()
    if len(parts) <= 1:
        return display_name
    return f"{parts[-1]}, {' '.join(parts[:-1])}"
