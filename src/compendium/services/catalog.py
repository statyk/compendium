from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from compendium.db.engine import get_settings
from compendium.domain.enums import CreatorRole, HoldStatus, ItemStatus, LoanRestrictionReason
from compendium.domain.errors import (
    BusinessRuleError,
    ExternalLookupError,
    NotFoundError,
    ValidationError,
)
from compendium.domain.identifiers import format_item_barcode
from compendium.domain.models import AppUser, Creator, Item, Work, WorkCreator
from compendium.repositories.base import (
    BranchRepository,
    CounterRepository,
    CreatorRepository,
    HoldRepository,
    ItemNoteRepository,
    ItemRepository,
    MediaTypeRepository,
    WorkRepository,
)
from compendium.services._normalization import compute_sort_title, normalize_creator_name
from compendium.services.audit import AuditAction, AuditEntityType, AuditService
from compendium.services.metadata import (
    get_book_primary_adapter_name,
    lookup_cover_fallbacks,
    lookup_metadata,
    lookup_metadata_from_source,
    lookup_metadata_with_source,
    normalize_isbn,
    normalize_upc,
    pick_classification_code,
)

_MISSING = object()


# Fields the refresh-metadata flow may update on a Work. Title is excluded
# (librarians treat their copy as authoritative; never overwrite). Authors
# are excluded for first cut (relationship handling is fiddly — refreshing
# them risks duplicate creators or order churn).
_REFRESHABLE_TEXT_FIELDS: tuple[str, ...] = (
    "subtitle",
    "publisher",
    "publication_year",
    "description",
    "language",
)


@dataclass
class RefreshReport:
    """Result of a refresh-metadata attempt.

    ``found=True`` means the upstream lookup succeeded. ``planned`` lists the
    fields that *would* change under fill-missing semantics (cover URL is
    the only field that may overwrite an existing value when upstream
    differs). ``cover_cache_busted`` is True when the proxy disk-cache file
    was removed (apply-mode only)."""

    work_id: int
    source: str | None  # "googlebooks" / "openlibrary" / "musicbrainz" / "tmdb" / None
    lookup_kind: str | None  # "isbn" / "upc" / "mbid" / "tmdb_id" / None
    lookup_value: str | None
    found: bool
    error: str | None = None
    # field_name -> (current_value, new_value)
    planned: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    applied: bool = False
    cover_cache_busted: bool = False


@dataclass
class BulkRefreshReport:
    """Aggregated result of a bulk metadata-refresh run.

    ``refreshed`` is "found upstream + at least one field changed" (apply
    mode) or "would have changed" (dry run). ``no_change`` means upstream
    returned data but the Work was already complete. ``not_found`` covers
    upstream misses + adapter errors. ``skipped_no_key`` is the count of
    Works that had no usable lookup identifier (rare with the repo filter
    but possible if a Work loses its ISBN between the query and the loop).
    """

    total_considered: int = 0
    refreshed: int = 0
    no_change: int = 0
    not_found: int = 0
    skipped_no_key: int = 0
    errored: int = 0
    sample_errors: list[str] = field(default_factory=list)
    dry_run: bool = False

_DEFAULT_CREATOR_ROLE: dict[str, str] = {
    "book": "author",
    "vinyl": "artist",
    "cd": "artist",
    "dvd": "director",
    "bluray": "director",
    "vhs": "director",
}

# Human-friendly source label per non-book media type — used in the refresh-
# metadata preview page header and audit details. Books are resolved dynamically
# via get_book_primary_adapter_name() so there is no "book" entry here.
_SOURCE_FOR_MEDIA_TYPE: dict[str, str] = {
    "vinyl": "musicbrainz",
    "cd": "musicbrainz",
    "dvd": "tmdb",
    "bluray": "tmdb",
    "vhs": "tmdb",
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
        hold_repo: HoldRepository | None = None,
        counter_repo: CounterRepository | None = None,
        item_note_repo: ItemNoteRepository | None = None,
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
        self._holds = hold_repo
        self._counters = counter_repo
        self._item_notes = item_note_repo

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

        _session = self._works._s
        meta = lookup_metadata(
            media_type_code, identifier_kind, identifier_value, session=_session
        )
        if not meta:
            raise ExternalLookupError(
                f"No metadata found for {identifier_kind} '{identifier_value}'. "
                "Check the identifier and try again."
            )

        if media_type_code == "book" and not meta.get("cover_image_url") and meta.get("isbn"):
            from compendium.services.metadata import get_book_primary_adapter_name
            from compendium.services.site_settings import get_site_setting

            meta["cover_image_url"] = lookup_cover_fallbacks(
                meta["isbn"],
                google_books_key=get_site_setting("google_books_api_key"),
                primary=get_book_primary_adapter_name(),
                session=_session,
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
                work.sort_title = compute_sort_title(new)
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

    def refresh_metadata(
        self,
        work_id: int,
        *,
        dry_run: bool = True,
        bypass_cache: bool = False,
        source: str | None = None,
    ) -> RefreshReport:
        """Re-fetch metadata for an existing Work and (optionally) apply.

        Strategy:
        - Pick a lookup key from the work — preferring ``external_ids``
          (mbid / tmdb_id) over isbn/upc, since those are more specific.
        - Call the appropriate adapter via ``lookup_metadata``. If ``source``
          is given, that specific adapter is used directly (bypassing the
          primary/fallback resolver); useful for per-source manual refresh.
        - Build a fill-missing diff for text fields (only update where the
          current value is null or empty).
        - Cover URL is exempt: replace if upstream has one and it differs
          from current. If upstream and current URL match, still bust the
          proxy cache on apply (the bytes at that URL may have changed).
        - On apply, force-bump ``work.updated_at`` so cover_url(version=)
          cache-busting kicks in even when only cover bytes changed.

        ``dry_run=True`` (default) returns the planned changes without
        committing. Use ``dry_run=False`` to apply.
        When ``source`` is set, ``bypass_cache`` defaults to True implicitly
        (caller is choosing a source precisely to bypass the cached primary).
        """
        work = self._works.get(work_id)
        if work is None:
            raise NotFoundError(f"No work with id {work_id}")

        media_type_code = work.media_type.code if work.media_type else None
        if media_type_code is None:
            return RefreshReport(
                work_id=work_id,
                source=None,
                lookup_kind=None,
                lookup_value=None,
                found=False,
                error="Work has no media type — cannot pick a metadata source.",
            )

        kind, value = self._pick_refresh_lookup_key(work, media_type_code)
        if kind is None:
            return RefreshReport(
                work_id=work_id,
                source=None,
                lookup_kind=None,
                lookup_value=None,
                found=False,
                error=(
                    "Work has no ISBN/UPC/MBID/TMDb ID to look up. "
                    "Add an external identifier to enable refresh."
                ),
            )

        intended_source = (
            source
            if source is not None
            else (
                get_book_primary_adapter_name()
                if media_type_code == "book"
                else _SOURCE_FOR_MEDIA_TYPE.get(media_type_code, "external")
            )
        )
        _session = self._works._s
        # When a specific source is requested, bypass cache by default — the
        # caller is explicitly choosing an alternative to the cached primary.
        effective_bypass = bypass_cache or (source is not None)
        try:
            if source is not None:
                data, actual_source = lookup_metadata_from_source(
                    media_type_code, kind, value, source,
                    session=_session, bypass_cache=effective_bypass,
                )
            else:
                data, actual_source = lookup_metadata_with_source(
                    media_type_code, kind, value,
                    bypass_cache=effective_bypass, session=_session,
                )
        except ValueError as exc:
            return RefreshReport(
                work_id=work_id,
                source=intended_source,
                lookup_kind=kind,
                lookup_value=value,
                found=False,
                error=str(exc),
            )
        except ExternalLookupError as exc:
            return RefreshReport(
                work_id=work_id,
                source=intended_source,
                lookup_kind=kind,
                lookup_value=value,
                found=False,
                error=str(exc),
            )
        source = actual_source or intended_source

        if not data:
            return RefreshReport(
                work_id=work_id,
                source=source,
                lookup_kind=kind,
                lookup_value=value,
                found=False,
                error="Upstream returned no data for this identifier.",
            )

        if media_type_code == "book" and not data.get("cover_image_url") and work.isbn:
            from compendium.services.site_settings import get_site_setting

            fallback = lookup_cover_fallbacks(
                work.isbn,
                google_books_key=get_site_setting("google_books_api_key"),
                primary=get_book_primary_adapter_name(),
                bypass_cache=bypass_cache,
                session=_session,
            )
            if fallback:
                data["cover_image_url"] = fallback

        planned = self._compute_refresh_diff(work, data)

        if dry_run:
            return RefreshReport(
                work_id=work_id,
                source=source,
                lookup_kind=kind,
                lookup_value=value,
                found=True,
                planned=planned,
            )

        # Apply.
        old_cover = work.cover_image_url
        new_cover = data.get("cover_image_url") or None

        for fname, (_old, new) in planned.items():
            setattr(work, fname, new)

        # Always force-bump updated_at so cover_url(version=) busts the
        # browser cache even when only cover bytes (not the URL) changed.
        work.updated_at = datetime.now(tz=UTC)
        result = self._works.update(work)

        # Bust the cover proxy disk cache for any URL that might be stale.
        cover_cache_busted = False
        from compendium.services import covers as _covers

        if old_cover:
            cover_cache_busted = _covers.invalidate(old_cover) or cover_cache_busted
        if new_cover and new_cover != old_cover:
            cover_cache_busted = _covers.invalidate(new_cover) or cover_cache_busted

        self._record(
            AuditEntityType.WORK,
            result.id,
            AuditAction.UPDATE,
            {
                "refreshed_from": source,
                "lookup_kind": kind,
                "lookup_value": value,
                "fields_updated": sorted(planned.keys()),
                "cover_cache_busted": cover_cache_busted,
            },
        )
        return RefreshReport(
            work_id=work_id,
            source=source,
            lookup_kind=kind,
            lookup_value=value,
            found=True,
            planned=planned,
            applied=True,
            cover_cache_busted=cover_cache_busted,
        )

    def refresh_metadata_bulk(
        self,
        *,
        media_type_code: str | None = None,
        branch_code: str | None = None,
        missing_only: bool = True,
        limit: int | None = None,
        dry_run: bool = True,
        on_progress: Callable[[int, int, Work, "RefreshReport | None"], None] | None = None,
    ) -> BulkRefreshReport:
        """Iterate Works that need metadata enrichment and refresh each one.

        Delegates per-Work work to ``refresh_metadata``; aggregates outcomes.
        Errors are caught and counted — the loop never aborts mid-batch so a
        cron run survives a flaky upstream. Emits one ``BULK_REFRESH_METADATA``
        audit entry on completion (apply mode only).

        ``on_progress`` (optional) is called once per iteration *after*
        bucketing, with ``(index, total, work, per_report)``. ``per_report``
        is the ``RefreshReport`` from ``refresh_metadata`` — or ``None`` if
        ``refresh_metadata`` raised (rare; existing path returns errors
        in-band). Service stays UI-free; the CLI uses this to stream live
        progress.
        """
        report = BulkRefreshReport(dry_run=dry_run)
        candidates = self._works.iter_for_refresh(
            media_type_code=media_type_code,
            branch_code=branch_code,
            missing_only=missing_only,
            limit=limit,
        )
        total = len(candidates)
        for index, work in enumerate(candidates, start=1):
            report.total_considered += 1
            per: "RefreshReport | None" = None
            try:
                per = self.refresh_metadata(work.id, dry_run=dry_run)
            except Exception as exc:  # defensive — refresh_metadata returns errors in-band
                report.errored += 1
                if len(report.sample_errors) < 20:
                    report.sample_errors.append(f"work {work.id}: {exc}")
                if on_progress is not None:
                    on_progress(index, total, work, None)
                continue
            if per.error:
                # refresh_metadata folds upstream errors and missing-key cases
                # into RefreshReport(found=False, error=...). Bucket them.
                if "no ISBN/UPC" in per.error or "no media type" in per.error:
                    report.skipped_no_key += 1
                else:
                    report.not_found += 1
            elif not per.found:
                report.not_found += 1
            elif per.planned:
                report.refreshed += 1
            else:
                report.no_change += 1
            if on_progress is not None:
                on_progress(index, total, work, per)

        if not dry_run and self._audit is not None:
            self._audit.record(
                actor=self._actor,
                actor_label=self._actor_label,
                source=self._source,
                entity_type=AuditEntityType.WORK,
                entity_id=None,
                action=AuditAction.BULK_REFRESH_METADATA,
                details={
                    "filters": {
                        "media_type_code": media_type_code,
                        "branch_code": branch_code,
                        "missing_only": missing_only,
                        "limit": limit,
                    },
                    "counts": {
                        "total_considered": report.total_considered,
                        "refreshed": report.refreshed,
                        "no_change": report.no_change,
                        "not_found": report.not_found,
                        "skipped_no_key": report.skipped_no_key,
                        "errored": report.errored,
                    },
                },
            )
        return report

    @staticmethod
    def _pick_refresh_lookup_key(
        work: Work, media_type_code: str
    ) -> tuple[str | None, str | None]:
        """Choose (kind, value) for an upstream lookup.

        Prefers identifier specificity: MBID/TMDb-ID over UPC over ISBN.
        """
        ext = work.external_ids or {}
        if media_type_code in ("vinyl", "cd") and ext.get("mbid"):
            return "mbid", str(ext["mbid"])
        if media_type_code in ("dvd", "bluray", "vhs") and ext.get("tmdb_id"):
            return "tmdb_id", str(ext["tmdb_id"])
        if work.isbn:
            return "isbn", work.isbn
        if work.upc:
            return "upc", work.upc
        return None, None

    @staticmethod
    def _compute_refresh_diff(work: Work, data: dict) -> dict[str, tuple[Any, Any]]:
        """Build the field-by-field plan under fill-missing-plus-cover rules."""
        planned: dict[str, tuple[Any, Any]] = {}
        for fname in _REFRESHABLE_TEXT_FIELDS:
            current = getattr(work, fname, None)
            new = data.get(fname)
            if not new:
                continue
            # Strict fill-missing: only update if current is None / empty.
            if current is None or (isinstance(current, str) and not current.strip()):
                planned[fname] = (current, new)

        # Cover URL — exempt from fill-missing. Replace when upstream differs.
        # (Classification refresh is intentionally skipped: it requires a
        # target scheme + can require a separate LoC lookup, and changes
        # rarely upstream. Out of scope here; can be added per-scheme later.)
        new_cover = data.get("cover_image_url")
        if new_cover and new_cover != work.cover_image_url:
            planned["cover_image_url"] = (work.cover_image_url, new_cover)

        return planned

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
        old_condition = item.condition
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
        if "condition" in changes:
            from compendium.services.item_notes import ItemNoteKind, record_system_note

            record_system_note(
                self._item_notes, item.id, ItemNoteKind.CONDITION.value,
                f"Condition changed from '{old_condition or '—'}' to '{changes['condition']}'.",
            )
        return result

    def replace_creators(
        self,
        work_id: int,
        creators: list[tuple[str, str]],
    ) -> Work:
        """Replace a work's full creators list.

        ``creators`` is ``[(display_name, role), ...]`` in the desired display
        order. An empty list removes all creators. Roles must be valid
        ``CreatorRole`` values. Duplicates by ``(sort_name, role)`` are rejected
        (the composite PK forbids them).
        """
        work = self._works.get(work_id)
        if work is None:
            raise NotFoundError(f"No Work with id={work_id}")

        valid_roles = {r.value for r in CreatorRole}
        cleaned: list[tuple[str, str]] = []
        seen_keys: set[tuple[str, str]] = set()
        for name, role in creators:
            name_clean = (name or "").strip()
            if not name_clean:
                raise ValidationError("Creator name is required.")
            if role not in valid_roles:
                raise ValidationError(
                    f"Unknown role '{role}'. Valid roles: {sorted(valid_roles)}"
                )
            key = (_to_sort_name(name_clean), role)
            if key in seen_keys:
                raise ValidationError(
                    f"Duplicate creator ({name_clean}, {role}) in list."
                )
            seen_keys.add(key)
            cleaned.append((name_clean, role))

        old_list = [(wc.creator.display_name, wc.role) for wc in work.creators]
        new_list = cleaned
        if old_list == new_list:
            return work

        work.creators.clear()
        for order, (name, role) in enumerate(cleaned):
            creator = self._get_or_create_creator(name)
            work.creators.append(
                WorkCreator(creator=creator, role=role, display_order=order)
            )

        self._rebuild_search_text(work)
        result = self._works.update(work)
        self._record(
            AuditEntityType.WORK, work.id, AuditAction.UPDATE,
            {"title": work.title, "creators": {"old": old_list, "new": new_list}},
        )
        return result

    def update_creator(
        self,
        creator_id: int,
        *,
        display_name: str,
    ) -> Creator:
        """Rename a Creator. Fans out a search_text rebuild to all linked works."""
        creator = self._creators.get(creator_id)
        if creator is None:
            raise NotFoundError(f"No Creator with id={creator_id}")
        new_display = (display_name or "").strip()
        if not new_display:
            raise ValidationError("Display name is required.")
        if new_display == creator.display_name:
            return creator

        new_sort = _to_sort_name(new_display)
        if new_sort != creator.sort_name:
            clash = self._creators.get_by_sort_name(new_sort)
            if clash is not None and clash.id != creator.id:
                raise BusinessRuleError(
                    f"Another creator already exists with sort_name '{new_sort}'. "
                    "Merging creators is not supported in this version."
                )

        old_display = creator.display_name
        creator.display_name = new_display
        creator.sort_name = new_sort
        result = self._creators.update(creator)

        for work in self._creators.list_works(creator.id):
            self._rebuild_search_text(work)
            self._works.update(work)

        self._record(
            AuditEntityType.CREATOR, creator.id, AuditAction.UPDATE,
            {"changes": {"display_name": {"old": old_display, "new": new_display}}},
        )
        return result

    def set_loanable(
        self,
        barcode: str,
        *,
        is_loanable: bool,
        reason: str | None = None,
        note: str | None = None,
    ) -> Item:
        """Flip an item's loanable flag. Enforces the enum/note invariants and,
        when flipping the last loanable copy of a work off, auto-cancels any
        WAITING holds on that work and drops an ON_HOLD item back to AVAILABLE."""
        item = self._items.get_by_barcode(barcode)
        if item is None:
            raise NotFoundError(f"No item with barcode '{barcode}'")

        if is_loanable:
            new_reason: str | None = None
            new_note: str | None = None
        else:
            if reason is None:
                raise ValidationError("A reason is required when marking an item non-loanable.")
            valid_reasons = {r.value for r in LoanRestrictionReason}
            if reason not in valid_reasons:
                raise ValidationError(
                    f"Unknown reason '{reason}'. Valid: {sorted(valid_reasons)}"
                )
            new_reason = reason
            clean_note = (note or "").strip() or None
            if reason == LoanRestrictionReason.OTHER.value:
                if not clean_note:
                    raise ValidationError("A note is required when reason is 'other'.")
                new_note = clean_note
            else:
                new_note = None

        old = (item.is_loanable, item.loan_restriction_reason, item.loan_restriction_note)
        new = (is_loanable, new_reason, new_note)
        if old == new:
            return item

        item.is_loanable = is_loanable
        item.loan_restriction_reason = new_reason
        item.loan_restriction_note = new_note
        # Flush so has_loanable_item() sees the updated flag (autoflush is off).
        result = self._items.update(item)

        cancelled_hold_ids: list[int] = []
        demoted_hold_ids: list[int] = []
        if not is_loanable and self._holds is not None:
            if not self._works.has_loanable_item(item.work_id):
                cancelled_hold_ids = self._cancel_work_holds(item.work_id)
                if item.status == ItemStatus.ON_HOLD.value:
                    item.status = ItemStatus.AVAILABLE.value
                    result = self._items.update(item)
            else:
                # Other loanable copies exist — keep the queue alive. But if
                # this specific copy was reserved for an AVAILABLE hold, that
                # hold is now pinned to a non-loanable item; demote it back
                # to WAITING so normal promotion picks a better copy later.
                demoted_hold_ids = self._demote_holds_pinned_to(item)
                if demoted_hold_ids and item.status == ItemStatus.ON_HOLD.value:
                    item.status = ItemStatus.AVAILABLE.value
                    result = self._items.update(item)

        details: dict[str, object] = {
            "barcode": item.barcode,
            "is_loanable": is_loanable,
            "reason": new_reason,
            "note": new_note,
        }
        if cancelled_hold_ids:
            details["auto_cancelled_hold_ids"] = cancelled_hold_ids
        if demoted_hold_ids:
            details["demoted_hold_ids"] = demoted_hold_ids
        self._record(AuditEntityType.ITEM, item.id, AuditAction.SET_LOANABLE, details)
        return result

    def _demote_holds_pinned_to(self, item: Item) -> list[int]:
        """If any AVAILABLE hold was pinned to ``item`` (via ``held_item_id``),
        demote it back to WAITING and unpin. Returns affected hold IDs."""
        assert self._holds is not None
        demoted: list[int] = []
        for hold in self._holds.get_active_for_work(item.work_id):
            if hold.held_item_id == item.id and hold.status == HoldStatus.AVAILABLE.value:
                hold.status = HoldStatus.WAITING.value
                hold.held_item_id = None
                hold.notified_at = None
                self._holds.update(hold)
                demoted.append(hold.id)
        return demoted

    def _cancel_work_holds(self, work_id: int) -> list[int]:
        """Cancel every non-terminal hold on a work. Returns cancelled hold ids."""
        assert self._holds is not None
        cancelled: list[int] = []
        for hold in self._holds.get_active_for_work(work_id):
            hold.status = HoldStatus.CANCELLED.value
            self._holds.update(hold)
            cancelled.append(hold.id)
        return cancelled

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
        from compendium.services.item_notes import ItemNoteKind, record_system_note

        record_system_note(
            self._item_notes, item.id, ItemNoteKind.STATUS.value,
            "Item withdrawn from circulation.",
        )
        return result

    def add_item_to_work(
        self,
        work_id: int,
        location: str | None = None,
        call_number: str | None = None,
        condition: str | None = None,
    ) -> Item:
        """Add another physical copy of an existing Work."""
        work = self._works.get(work_id)
        if work is None:
            raise NotFoundError(f"No Work with id={work_id}")
        item = self._create_item(work, location=location, call_number=call_number, condition=condition)
        self._record(
            AuditEntityType.ITEM, item.id, AuditAction.CREATE,
            {"snapshot": {"barcode": item.barcode, "work_id": work.id}},
        )
        return item

    def add_from_import(
        self,
        *,
        media_type_code: str,
        meta: dict,
        conflict_mode: str = "append",
        barcode: str | None = None,
        accession_number: str | None = None,
        call_number: str | None = None,
        condition: str | None = None,
        location: str | None = None,
        branch_code: str | None = None,
        is_loanable: bool = True,
        loan_restriction_reason: str | None = None,
        loan_restriction_note: str | None = None,
    ) -> tuple[Work | None, Item | None, str]:
        """Import-dedicated entry point. Dedups by ISBN/UPC, honours conflict_mode
        ('append' | 'skip-duplicates' | 'error-on-conflict'), and propagates
        import-specific item fields (pre-set barcode, loanable state).
        Does NOT emit per-row audits — the caller records a summary BULK_IMPORT
        entry. Returns (work, item, outcome) where outcome is one of:
        'created_work', 'added_copy', 'skipped_duplicate', 'errored_on_conflict'."""
        title = (meta.get("title") or "").strip()
        if not title:
            raise ValidationError("Title is required.")
        meta = {**meta, "title": title}

        branch = None
        if branch_code:
            branch = self._branches.get_by_code(branch_code)
            if branch is None:
                raise ValidationError(f"Unknown branch code '{branch_code}'")

        existing: Work | None = None
        if meta.get("isbn"):
            existing = self._works.get_by_isbn(meta["isbn"])
        if existing is None and meta.get("upc"):
            existing = self._works.get_by_upc(meta["upc"])

        item_kwargs = {
            "barcode": barcode,
            "accession_number": accession_number,
            "call_number": call_number,
            "condition": condition,
            "location": location,
            "is_loanable": is_loanable,
            "loan_restriction_reason": loan_restriction_reason,
            "loan_restriction_note": loan_restriction_note,
        }

        if existing is not None:
            if conflict_mode == "skip-duplicates":
                return existing, None, "skipped_duplicate"
            if conflict_mode == "error-on-conflict":
                return existing, None, "errored_on_conflict"
            item = self._create_item(existing, branch=branch, **item_kwargs)
            return existing, item, "added_copy"

        work = self._create_work(meta, media_type_code, branch=branch)
        item = self._create_item(work, branch=branch, **item_kwargs)
        return work, item, "created_work"

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
        if mt is None:
            raise ValidationError(f"Unknown media_type '{media_type_code}'")

        work = Work(
            title=meta["title"],
            sort_title=compute_sort_title(meta["title"]),
            subtitle=meta.get("subtitle"),
            media_type_id=mt.id,
            publisher=meta.get("publisher"),
            publication_year=meta.get("publication_year"),
            language=meta.get("language") or "en",
            description=meta.get("description"),
            isbn=meta.get("isbn"),
            upc=meta.get("upc"),
            cover_image_url=meta.get("cover_image_url"),
            external_ids=meta.get("external_ids", {}),
            extra_metadata=meta.get("extra_metadata", {}),
        )

        # Explicit classification (e.g., from CSV/MARC import) wins over branch defaults.
        if meta.get("classification_scheme") and meta.get("classification_code"):
            work.classification_scheme = meta["classification_scheme"]
            work.classification_code = meta["classification_code"]
        else:
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
        display_name = normalize_creator_name(display_name.strip())
        sort_name = _to_sort_name(display_name)
        creator = self._creators.get_by_sort_name(sort_name)
        if creator is None:
            creator = Creator(display_name=display_name, sort_name=sort_name)
            self._creators.add(creator)
        return creator

    def _create_item(
        self,
        work: Work,
        location: str | None = None,
        branch=None,
        *,
        barcode: str | None = None,
        accession_number: str | None = None,
        call_number: str | None = None,
        condition: str | None = None,
        is_loanable: bool = True,
        loan_restriction_reason: str | None = None,
        loan_restriction_note: str | None = None,
    ) -> Item:
        if branch is None:
            branch = self._branches.get_default()
        if accession_number is None:
            accession_number = self._next_accession()
        if barcode is None:
            loc = self._resolve_location_code(branch)
            barcode = format_item_barcode(accession_number, location_code=loc)
        item = Item(
            work_id=work.id,
            branch_id=branch.id,  # type: ignore[union-attr]
            barcode=barcode,
            accession_number=accession_number,
            location=location,
            call_number=call_number,
            condition=condition,
            is_loanable=is_loanable,
            loan_restriction_reason=loan_restriction_reason,
            loan_restriction_note=loan_restriction_note,
        )
        return self._items.add(item)

    def _next_accession(self) -> str:
        if self._counters is not None:
            n = self._counters.next("catalog.accession")
        else:
            # Fallback for test fixtures that don't inject a CounterRepository.
            # Production call sites always pass one.
            n = self._items.count_all() + 1
        return f"{n:08d}"

    def _resolve_location_code(self, branch) -> str | None:
        # Read settings through the current session (via the item repo) to avoid
        # opening a second DB connection, which would interfere with StaticPool
        # sessions used in tests.
        db = getattr(self._items, "_s", None)
        if db is not None:
            return self._resolve_location_via_session(db, branch)
        from compendium.services.site_settings import get_site_setting

        if get_site_setting("barcode_format") != "14-digit":
            return None
        if branch is not None and getattr(branch, "location_code", None):
            return branch.location_code
        return get_site_setting("barcode_default_location_code")

    @staticmethod
    def _resolve_location_via_session(session, branch) -> str | None:
        import os

        from compendium.repositories.sql.site_setting_repository import (
            SqlSiteSettingRepository,
        )
        from compendium.services.settings_registry import get_descriptor
        from compendium.services.settings_registry import parse as parse_setting

        repo = SqlSiteSettingRepository(session)

        def _read(key: str):
            desc = get_descriptor(key)
            env_val = desc.env_value()
            if env_val is not None:
                return parse_setting(desc, env_val)
            row = repo.get(key)
            if row is not None:
                return parse_setting(desc, row.value)
            return desc.default

        if _read("barcode_format") != "14-digit":
            return None
        if branch is not None and getattr(branch, "location_code", None):
            return branch.location_code
        return _read("barcode_default_location_code")

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
