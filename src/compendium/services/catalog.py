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
from compendium.services.metadata import lookup_lcc_from_loc, lookup_metadata, normalize_isbn

_SCHEME_TO_META_KEY = {"lcc": "lc_classification", "ddc": "ddc_classification"}


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

    def add_from_lookup(
        self,
        media_type_code: str,
        identifier_kind: str,
        identifier_value: str,
        location: str | None = None,
    ) -> tuple[Work, Item]:
        """Look up an item via the appropriate metadata adapter and add Work + Item.

        If a Work with this identifier already exists, adds a new copy instead.
        """
        branch = self._branches.get_default()

        existing = self._find_existing_work(identifier_kind, identifier_value)
        if existing is not None:
            item = self._create_item(existing, location=location, branch=branch)
            self._record(
                AuditEntityType.ITEM, item.id, AuditAction.CREATE,
                {"snapshot": {"barcode": item.barcode, "work_id": existing.id}},
            )
            return existing, item

        meta = lookup_metadata(media_type_code, identifier_kind, identifier_value)
        if not meta:
            raise ExternalLookupError(
                f"No metadata found for {identifier_kind} '{identifier_value}'. "
                "Check the identifier and try again."
            )

        # For MBID lookups the returned meta may carry a UPC — check for an
        # existing Work by that UPC to avoid duplicate records.
        if identifier_kind == "mbid" and meta.get("upc"):
            existing = self._works.get_by_upc(meta["upc"])
            if existing is not None:
                item = self._create_item(existing, location=location, branch=branch)
                self._record(
                    AuditEntityType.ITEM, item.id, AuditAction.CREATE,
                    {"snapshot": {"barcode": item.barcode, "work_id": existing.id}},
                )
                return existing, item

        work = self._create_work(meta, media_type_code, branch=branch)
        item = self._create_item(work, location=location, branch=branch)
        self._record(
            AuditEntityType.WORK, work.id, AuditAction.CREATE,
            {"snapshot": {"title": work.title, "isbn": work.isbn, "upc": work.upc}},
        )
        self._record(
            AuditEntityType.ITEM, item.id, AuditAction.CREATE,
            {"snapshot": {"barcode": item.barcode, "work_id": work.id}},
        )
        return work, item

    def add_from_isbn(
        self,
        raw_isbn: str,
        location: str | None = None,
    ) -> tuple[Work, Item]:
        """Convenience wrapper: normalise ISBN then delegate to add_from_lookup."""
        isbn = normalize_isbn(raw_isbn)
        return self.add_from_lookup("book", "isbn", isbn, location=location)

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

    def _find_existing_work(self, kind: str, value: str) -> Work | None:
        if kind == "isbn":
            return self._works.get_by_isbn(value)
        if kind == "upc":
            return self._works.get_by_upc(value)
        return None

    def _create_work(self, meta: dict, media_type_code: str, branch=None) -> Work:
        mt = self._media_types.get_by_code(media_type_code)

        work = Work(
            title=meta["title"],
            subtitle=meta.get("subtitle"),
            media_type_id=mt.id,  # type: ignore[union-attr]
            publisher=meta.get("publisher"),
            publication_year=meta.get("publication_year"),
            language="en",
            description=meta.get("description"),
            isbn=meta.get("isbn"),
            upc=meta.get("upc"),
            cover_image_url=meta.get("cover_image_url"),
            external_ids=meta.get("external_ids", {}),
            extra_metadata=meta.get("extra_metadata", {}),
        )

        scheme = (branch.default_classification_scheme if branch else None) or "none"
        if scheme != "none":
            meta_key = _SCHEME_TO_META_KEY.get(scheme)
            if meta_key:
                code = meta.get(meta_key)
                if not code and scheme == "lcc":
                    code = lookup_lcc_from_loc(
                        isbn=meta.get("isbn") or "",
                        lccn=meta.get("lccn"),
                    )
                if code:
                    work.classification_scheme = scheme
                    work.classification_code = code
        self._works.add(work)

        # "creators" key carries [(name, role), ...] for multi-role media (film).
        # Falls back to flat "authors" + single "creator_role" for books/music.
        if meta.get("creators"):
            for order, (name, role) in enumerate(meta["creators"]):
                creator = self._get_or_create_creator(name)
                work.creators.append(
                    WorkCreator(creator=creator, role=role, display_order=order)
                )
        else:
            creator_role = meta.get("creator_role", "author")
            for order, name in enumerate(meta.get("authors", [])):
                creator = self._get_or_create_creator(name)
                work.creators.append(
                    WorkCreator(creator=creator, role=creator_role, display_order=order)
                )

        self._rebuild_search_text(work)
        return work

    def _rebuild_search_text(self, work: Work) -> None:
        parts = [work.title or "", work.subtitle or "", work.description or ""]
        parts += [wc.creator.display_name for wc in work.creators]
        work.search_text = " ".join(p.strip() for p in parts if p and p.strip())

    def _get_or_create_creator(self, display_name: str) -> Creator:
        sort_name = _to_sort_name(display_name)
        creator = self._creators.get_by_sort_name(sort_name)
        if creator is None:
            creator = Creator(display_name=display_name, sort_name=sort_name)
            self._creators.add(creator)
        return creator

    def _create_item(self, work: Work, location: str | None = None, branch=None) -> Item:
        if branch is None:
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
