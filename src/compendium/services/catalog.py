from compendium.domain.enums import ItemStatus
from compendium.domain.errors import (
    BusinessRuleError,
    ExternalLookupError,
    NotFoundError,
    ValidationError,
)
from compendium.domain.models import AppUser, Creator, Item, Work, WorkCreator
from compendium.repositories.base import (
    BranchRepository,
    CreatorRepository,
    ItemRepository,
    MediaTypeRepository,
    WorkRepository,
)
from compendium.services.audit import AuditAction, AuditEntityType, AuditService
from compendium.services.metadata import (
    lookup_metadata,
    normalize_isbn,
    normalize_upc,
    pick_classification_code,
)

_MISSING = object()

_DEFAULT_CREATOR_ROLE: dict[str, str] = {
    "book": "author",
    "vinyl": "artist",
    "cd": "artist",
    "dvd": "director",
    "bluray": "director",
    "vhs": "director",
}


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

    def add_manual(
        self,
        media_type_code: str,
        title: str,
        *,
        authors: list[str] | None = None,
        publisher: str | None = None,
        publication_year: int | None = None,
        isbn: str | None = None,
        upc: str | None = None,
        description: str | None = None,
        location: str | None = None,
    ) -> tuple[Work, Item]:
        """Create a Work + Item from manually-entered fields (no external lookup).

        If ``isbn`` or ``upc`` is supplied and a matching Work already exists,
        adds a new copy to that Work rather than creating a duplicate.
        """
        if not title or not title.strip():
            raise ValidationError("Title is required.")
        norm_isbn = normalize_isbn(isbn) if isbn else None
        norm_upc = normalize_upc(upc) if upc else None

        branch = self._branches.get_default()

        existing: Work | None = None
        if norm_isbn:
            existing = self._works.get_by_isbn(norm_isbn)
        if existing is None and norm_upc:
            existing = self._works.get_by_upc(norm_upc)
        if existing is not None:
            item = self._create_item(existing, location=location, branch=branch)
            self._record(
                AuditEntityType.ITEM, item.id, AuditAction.CREATE,
                {"snapshot": {"barcode": item.barcode, "work_id": existing.id}},
            )
            return existing, item

        meta: dict = {
            "title": title.strip(),
            "subtitle": None,
            "authors": [a.strip() for a in (authors or []) if a and a.strip()],
            "creator_role": _DEFAULT_CREATOR_ROLE.get(media_type_code, "author"),
            "publisher": publisher.strip() if publisher else None,
            "publication_year": publication_year,
            "description": description.strip() if description else None,
            "cover_image_url": None,
            "isbn": norm_isbn,
            "upc": norm_upc,
            "external_ids": {},
            "extra_metadata": {},
        }
        work = self._create_work(meta, media_type_code, branch=branch)
        item = self._create_item(work, location=location, branch=branch)
        self._record(
            AuditEntityType.WORK, work.id, AuditAction.CREATE,
            {"snapshot": {"title": work.title, "isbn": work.isbn, "upc": work.upc, "manual": True}},
        )
        self._record(
            AuditEntityType.ITEM, item.id, AuditAction.CREATE,
            {"snapshot": {"barcode": item.barcode, "work_id": work.id}},
        )
        return work, item

    def update_work(
        self,
        work_id: int,
        *,
        title: str = _MISSING,  # type: ignore[assignment]
        subtitle: str | None = _MISSING,  # type: ignore[assignment]
        publisher: str | None = _MISSING,  # type: ignore[assignment]
        publication_year: int | None = _MISSING,  # type: ignore[assignment]
        edition: str | None = _MISSING,  # type: ignore[assignment]
        language: str | None = _MISSING,  # type: ignore[assignment]
        description: str | None = _MISSING,  # type: ignore[assignment]
        classification_scheme: str | None = _MISSING,  # type: ignore[assignment]
        classification_code: str | None = _MISSING,  # type: ignore[assignment]
        cover_image_url: str | None = _MISSING,  # type: ignore[assignment]
    ) -> Work:
        """Update editable fields on a Work. ISBN, UPC, media_type, creators,
        external_ids, and extra_metadata are intentionally NOT editable here."""
        work = self._works.get(work_id)
        if work is None:
            raise NotFoundError(f"No Work with id={work_id}")

        def _norm(v):
            if isinstance(v, str):
                s = v.strip()
                return s if s else None
            return v

        changes: dict[str, object | None] = {}
        search_text_dirty = False

        if title is not _MISSING:
            new = _norm(title)
            if not new:
                raise ValidationError("Title is required.")
            if new != work.title:
                work.title = new
                changes["title"] = new
                search_text_dirty = True
        if subtitle is not _MISSING:
            new = _norm(subtitle)
            if new != work.subtitle:
                work.subtitle = new
                changes["subtitle"] = new
                search_text_dirty = True
        if publisher is not _MISSING:
            new = _norm(publisher)
            if new != work.publisher:
                work.publisher = new
                changes["publisher"] = new
        if publication_year is not _MISSING:
            new = publication_year
            if new != work.publication_year:
                work.publication_year = new
                changes["publication_year"] = new
        if edition is not _MISSING:
            new = _norm(edition)
            if new != work.edition:
                work.edition = new
                changes["edition"] = new
        if language is not _MISSING:
            new = _norm(language)
            if new != work.language:
                work.language = new
                changes["language"] = new
        if description is not _MISSING:
            new = _norm(description)
            if new != work.description:
                work.description = new
                changes["description"] = new
                search_text_dirty = True
        if classification_scheme is not _MISSING:
            new = _norm(classification_scheme)
            if new != work.classification_scheme:
                work.classification_scheme = new
                changes["classification_scheme"] = new
        if classification_code is not _MISSING:
            new = _norm(classification_code)
            if new != work.classification_code:
                work.classification_code = new
                changes["classification_code"] = new
        if cover_image_url is not _MISSING:
            new = _norm(cover_image_url)
            if new != work.cover_image_url:
                work.cover_image_url = new
                changes["cover_image_url"] = new

        if not changes:
            return work

        if search_text_dirty:
            self._rebuild_search_text(work)

        result = self._works.update(work)
        self._record(
            AuditEntityType.WORK, work.id, AuditAction.UPDATE,
            {"title": work.title, "changes": changes},
        )
        return result

    def update_item(
        self,
        barcode: str,
        *,
        location: str | None = _MISSING,  # type: ignore[assignment]
        call_number: str | None = _MISSING,  # type: ignore[assignment]
        condition: str | None = _MISSING,  # type: ignore[assignment]
        notes: str | None = _MISSING,  # type: ignore[assignment]
    ) -> Item:
        """Update editable fields on an item. Pass a value (or None to clear);
        omit a kwarg to leave that field untouched."""
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")

        changes: dict[str, object | None] = {}
        if location is not _MISSING:
            new = location.strip() if isinstance(location, str) and location.strip() else None
            if new != item.location:
                item.location = new
                changes["location"] = new
        if call_number is not _MISSING:
            new = call_number.strip() if isinstance(call_number, str) and call_number.strip() else None
            if new != item.call_number:
                item.call_number = new
                changes["call_number"] = new
        if condition is not _MISSING:
            new = condition.strip() if isinstance(condition, str) and condition.strip() else None
            if new != item.condition:
                item.condition = new
                changes["condition"] = new
        if notes is not _MISSING:
            new = notes.strip() if isinstance(notes, str) and notes.strip() else None
            if new != item.notes:
                item.notes = new
                changes["notes"] = new

        if not changes:
            return item

        result = self._items.update(item)
        self._record(
            AuditEntityType.ITEM, item.id, AuditAction.UPDATE,
            {"barcode": item.barcode, "changes": changes},
        )
        return result

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
            code = pick_classification_code(scheme, meta)
            if code:
                work.classification_scheme = scheme
                work.classification_code = code
        self._works.add(work)

        # "creators" key carries [(name, role), ...] for multi-role media (film).
        # Falls back to flat "authors" + single "creator_role" for books/music.
        if meta.get("creators"):
            pairs = [(name, role) for name, role in meta["creators"]]
        else:
            creator_role = meta.get("creator_role", "author")
            pairs = [(name, creator_role) for name in meta.get("authors", [])]

        # External sources (e.g. Open Library) occasionally list the same author
        # twice; dedupe by (sort_name, role) so the work_creator PK isn't violated.
        seen: set[tuple[str, str]] = set()
        order = 0
        for name, role in pairs:
            creator = self._get_or_create_creator(name)
            key = (creator.sort_name, role)
            if key in seen:
                continue
            seen.add(key)
            work.creators.append(
                WorkCreator(creator=creator, role=role, display_order=order)
            )
            order += 1

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
