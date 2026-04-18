from compendium.domain.errors import ExternalLookupError, NotFoundError
from compendium.domain.models import Creator, Item, Work, WorkCreator
from compendium.repositories.base import (
    BranchRepository,
    CreatorRepository,
    ItemRepository,
    WorkRepository,
)
from compendium.services.metadata import lookup_isbn, normalize_isbn, parse_open_library


class CatalogService:
    def __init__(
        self,
        work_repo: WorkRepository,
        item_repo: ItemRepository,
        creator_repo: CreatorRepository,
        branch_repo: BranchRepository,
    ) -> None:
        self._works = work_repo
        self._items = item_repo
        self._creators = creator_repo
        self._branches = branch_repo

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
        return work, item

    def add_item_to_work(self, work_id: int, location: str | None = None) -> Item:
        """Add another physical copy of an existing Work."""
        work = self._works.get(work_id)
        if work is None:
            raise NotFoundError(f"No Work with id={work_id}")
        return self._create_item(work, location=location)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _create_work(self, meta: dict) -> Work:
        from compendium.domain.models import MediaType
        # Resolve book media type — we created seed data so it will exist.
        # The repo doesn't have a media_type query yet; use the session via
        # the item_repo's session. For now, look up by querying through
        # relationships will be handled by SQLAlchemy once flush is done.
        # We rely on the caller's session to have MediaType pre-loaded.

        # Get "book" media type id — done by querying via item_repo's underlying session.
        # Because our repos wrap a session, we reach into the session here.
        # This is pragmatic for v1; a MediaTypeRepository is the clean solution.
        session = self._items._s  # type: ignore[attr-defined]
        book_mt = session.query(MediaType).filter_by(code="book").first()

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
            work.creators.append(
                WorkCreator(creator=creator, role="author", display_order=order)
            )

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


def _to_sort_name(display_name: str) -> str:
    """'Frank Herbert' → 'Herbert, Frank'  (simple last-word heuristic)."""
    parts = display_name.strip().split()
    if len(parts) <= 1:
        return display_name
    return f"{parts[-1]}, {' '.join(parts[:-1])}"
