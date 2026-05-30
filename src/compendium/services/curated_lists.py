from __future__ import annotations

import re

from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser, CuratedList, CuratedListEntry
from compendium.repositories.base import CuratedListRepository, WorkRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService

_MISSING = object()


def _make_slug(name: str) -> str:
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:96]


class CuratedListService:
    def __init__(
        self,
        curated_list_repo: CuratedListRepository,
        work_repo: WorkRepository,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._lists = curated_list_repo
        self._works = work_repo
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

    # ------------------------------------------------------------------
    # List-level operations
    # ------------------------------------------------------------------

    def create(
        self,
        name: str,
        *,
        description: str | None = None,
        is_public: bool = True,
        is_featured: bool = False,
        display_order: int = 0,
    ) -> CuratedList:
        if not name or not name.strip():
            raise ValidationError("Curated list name cannot be blank.")
        base = _make_slug(name)
        if not base:
            raise ValidationError("Name must contain at least one letter or digit.")
        slug = self._unique_slug(name)
        cl = CuratedList(
            slug=slug,
            name=name.strip(),
            description=description,
            is_public=is_public,
            is_featured=is_featured,
            display_order=display_order,
        )
        result = self._lists.add(cl)
        self._record(result.id, AuditAction.CREATE, {"name": result.name, "slug": result.slug})
        return result

    def get(self, list_id: int) -> CuratedList:
        cl = self._lists.get(list_id)
        if cl is None:
            raise NotFoundError(f"Curated list {list_id} not found.")
        return cl

    def get_by_slug(self, slug: str) -> CuratedList:
        cl = self._lists.get_by_slug(slug)
        if cl is None:
            raise NotFoundError(f"Curated list with slug '{slug}' not found.")
        return cl

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        public_only: bool = False,
        featured_only: bool = False,
    ) -> list[CuratedList]:
        return self._lists.list(
            limit=limit,
            offset=offset,
            public_only=public_only,
            featured_only=featured_only,
        )

    def count(self, *, public_only: bool = False, featured_only: bool = False) -> int:
        return self._lists.count(public_only=public_only, featured_only=featured_only)

    def update(
        self,
        list_id: int,
        *,
        name: str | object = _MISSING,
        description: str | None | object = _MISSING,
        is_public: bool | object = _MISSING,
        is_featured: bool | object = _MISSING,
        display_order: int | object = _MISSING,
        slug: str | object = _MISSING,
    ) -> CuratedList:
        cl = self.get(list_id)
        changes: dict = {}

        # Validate name first if provided
        if name is not _MISSING:
            if not name or not str(name).strip():
                raise ValidationError("Curated list name cannot be blank.")
            cl.name = str(name).strip()
            changes["name"] = cl.name

        # Regenerate slug if name changed and slug not explicitly provided
        if name is not _MISSING and slug is _MISSING:
            new_base = _make_slug(cl.name)
            if not new_base:
                raise ValidationError("Name must contain at least one letter or digit.")
            new_slug = self._unique_slug(cl.name, exclude_id=list_id)
            cl.slug = new_slug
            changes["slug"] = cl.slug
        elif slug is not _MISSING:
            normalized = _make_slug(str(slug))
            if not normalized:
                raise ValidationError("Slug must contain at least one letter or digit.")
            cl.slug = normalized
            changes["slug"] = normalized

        if description is not _MISSING:
            cl.description = description  # type: ignore[assignment]
            changes["description"] = description
        if is_public is not _MISSING:
            cl.is_public = is_public  # type: ignore[assignment]
            changes["is_public"] = is_public
        if is_featured is not _MISSING:
            cl.is_featured = is_featured  # type: ignore[assignment]
            changes["is_featured"] = is_featured
        if display_order is not _MISSING:
            cl.display_order = display_order  # type: ignore[assignment]
            changes["display_order"] = display_order

        result = self._lists.update(cl)
        if changes:
            self._record(list_id, AuditAction.UPDATE, changes)
        return result

    def delete(self, list_id: int) -> None:
        cl = self.get(list_id)
        self._record(list_id, AuditAction.DELETE, {"name": cl.name, "slug": cl.slug})
        self._lists.delete(cl)

    # ------------------------------------------------------------------
    # Entry-level operations
    # ------------------------------------------------------------------

    def add_work(
        self,
        list_id: int,
        work_id: int,
        annotation: str | None = None,
    ) -> CuratedListEntry:
        self.get(list_id)
        work = self._works.get(work_id)
        if work is None:
            raise NotFoundError(f"Work {work_id} not found.")
        existing = self._lists.get_entry(list_id, work_id)
        if existing is not None:
            raise BusinessRuleError("Work already in this list.")
        order = self._lists.max_entry_order(list_id) + 1
        entry = CuratedListEntry(
            list_id=list_id,
            work_id=work_id,
            display_order=order,
            annotation=annotation,
        )
        result = self._lists.add_entry(entry)
        self._record(
            list_id,
            AuditAction.LIST_ADD_WORK,
            {"work_id": work_id, "display_order": order},
        )
        return result

    def remove_work(self, list_id: int, work_id: int) -> None:
        self.get(list_id)
        entry = self._lists.get_entry(list_id, work_id)
        if entry is None:
            raise NotFoundError("Work not in this list.")
        self._lists.remove_entry(list_id, work_id)
        self._record(list_id, AuditAction.LIST_REMOVE_WORK, {"work_id": work_id})

    def reorder(self, list_id: int, ordered_work_ids: list[int]) -> CuratedList:
        cl = self.get(list_id)
        known_ids = {e.work_id for e in cl.entries}
        unknown = set(ordered_work_ids) - known_ids
        if unknown:
            raise BusinessRuleError(
                f"Unknown work_id(s) for this list: {sorted(unknown)}"
            )
        for pos, work_id in enumerate(ordered_work_ids):
            entry = self._lists.get_entry(list_id, work_id)
            if entry is not None:
                entry.display_order = pos
                self._lists.update_entry(entry)
        cl.entries.sort(key=lambda e: e.display_order)
        return cl

    def set_annotation(
        self,
        list_id: int,
        work_id: int,
        annotation: str | None,
    ) -> CuratedListEntry:
        self.get(list_id)
        entry = self._lists.get_entry(list_id, work_id)
        if entry is None:
            raise NotFoundError("Work not in this list.")
        entry.annotation = annotation
        return self._lists.update_entry(entry)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _unique_slug(self, name: str, exclude_id: int | None = None) -> str:
        _MAX_DEDUP = 1000
        base = _make_slug(name)
        candidate = base
        suffix = 2
        while True:
            exists = self._lists.slug_exists(candidate)
            if not exists:
                return candidate
            # If the existing slug belongs to the entity being updated, it's fine
            if exclude_id is not None:
                existing = self._lists.get_by_slug(candidate)
                if existing is not None and existing.id == exclude_id:
                    return candidate
            if suffix > _MAX_DEDUP:
                raise BusinessRuleError(
                    f"Could not generate a unique slug for '{base}' after {_MAX_DEDUP} attempts."
                )
            max_base = 96 - len(str(suffix)) - 1  # -1 for the hyphen
            candidate = f"{base[:max_base]}-{suffix}"
            suffix += 1

    def _record(
        self,
        entity_id: int | None,
        action: str,
        details: dict | None = None,
    ) -> None:
        if self._audit is not None:
            self._audit.record(
                actor=self._actor,
                actor_label=self._actor_label,
                source=self._source,
                entity_type=AuditEntityType.CURATED_LIST,
                entity_id=entity_id,
                action=action,
                details=details,
            )
