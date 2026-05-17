"""Discovery — search facets + landing-page lists (new arrivals, recently returned)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from compendium.domain.models import Work
from compendium.repositories.base import MediaTypeRepository, WorkRepository


@dataclass
class FacetCounts:
    media_type: list[tuple[str, str, int]] = field(default_factory=list)  # (code, name, n)
    decade: list[tuple[int, int]] = field(default_factory=list)  # (decade, n)
    available_now: int = 0


@dataclass
class SearchPage:
    works: list[Work]
    total: int
    facets: FacetCounts
    page: int
    page_size: int
    availability: dict[int, str] = field(default_factory=dict)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page * self.page_size < self.total


class DiscoveryService:
    def __init__(
        self,
        *,
        work_repo: WorkRepository,
        media_type_repo: MediaTypeRepository | None = None,
    ) -> None:
        self._works = work_repo
        self._media_types = media_type_repo

    def search(
        self,
        q: str,
        field: str = "all",
        *,
        page: int = 1,
        page_size: int = 25,
        media_type_codes: list[str] | None = None,
        decade: int | None = None,
        available_only: bool = False,
        include_withdrawn_only: bool = False,
        order_by: Literal["title", "author", "recent", "relevance"] = "title",
    ) -> SearchPage:
        page = max(1, page)
        offset = (page - 1) * page_size
        works = self._works.search(
            q,
            field=field,
            limit=page_size,
            offset=offset,
            media_type_codes=media_type_codes or None,
            decade=decade,
            available_only=available_only,
            include_withdrawn_only=include_withdrawn_only,
            order_by=order_by,
        )
        total = self._works.count_search(
            q,
            field=field,
            media_type_codes=media_type_codes or None,
            decade=decade,
            available_only=available_only,
            include_withdrawn_only=include_withdrawn_only,
        )
        facets = self.facet_counts(
            q,
            field=field,
            media_type_codes=media_type_codes,
            decade=decade,
            available_only=available_only,
            include_withdrawn_only=include_withdrawn_only,
        )
        availability = self._works.availability_for_works([w.id for w in works])
        return SearchPage(
            works=works,
            total=total,
            facets=facets,
            page=page,
            page_size=page_size,
            availability=availability,
        )

    def facet_counts(
        self,
        q: str,
        field: str = "all",
        *,
        media_type_codes: list[str] | None = None,
        decade: int | None = None,
        available_only: bool = False,
        include_withdrawn_only: bool = False,
    ) -> FacetCounts:
        # For each facet, drop that facet's own selection so the user can see
        # alternatives within the group (standard faceted-search UX).
        return FacetCounts(
            media_type=self._works.facet_media_counts(
                q, field, decade=decade, available_only=available_only,
                include_withdrawn_only=include_withdrawn_only,
            ),
            decade=self._works.facet_decade_counts(
                q,
                field,
                media_type_codes=media_type_codes or None,
                available_only=available_only,
                include_withdrawn_only=include_withdrawn_only,
            ),
            available_now=self._works.facet_available_count(
                q,
                field,
                media_type_codes=media_type_codes or None,
                decade=decade,
                include_withdrawn_only=include_withdrawn_only,
            ),
        )

    def suggest(self, q: str, *, limit: int = 8) -> list[Work]:
        return self._works.suggest(q, limit=limit)

    def new_arrivals(self, *, days: int = 60, limit: int = 12, include_withdrawn_only: bool = False) -> list[Work]:
        return self._works.list_recent(days=days, limit=limit, include_withdrawn_only=include_withdrawn_only)

    def recently_returned(self, *, days: int = 30, limit: int = 12, include_withdrawn_only: bool = False) -> list[Work]:
        return self._works.list_recently_returned(days=days, limit=limit, include_withdrawn_only=include_withdrawn_only)
