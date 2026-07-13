from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, case, exists, func, or_, text
from sqlalchemy.orm import Session, aliased, selectinload

from compendium.domain.enums import ItemStatus
from compendium.domain.models import Branch, Creator, Item, Loan, MediaType, Work, WorkCreator
from compendium.repositories.base import WorkAvailability

_TOKEN_SANITIZE = re.compile(r"[^A-Za-z0-9'-]+")


class SqlWorkRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, work: Work) -> Work:
        self._s.add(work)
        self._s.flush()
        return work

    def get(self, id: int) -> Work | None:
        return self._s.get(Work, id)

    def update(self, work: Work) -> Work:
        self._s.flush()
        return work

    def get_by_isbn(self, isbn: str) -> Work | None:
        return self._s.query(Work).filter_by(isbn=isbn).first()

    def get_by_upc(self, upc: str) -> Work | None:
        return self._s.query(Work).filter_by(upc=upc).first()

    def has_loanable_item(self, work_id: int) -> bool:
        # Whitelist statuses where the copy will realistically circulate again.
        # LOST / DAMAGED / WITHDRAWN copies shouldn't count, so a hold placed
        # on a work with only such copies would otherwise wait forever.
        # CLAIMS_RETURNED is included as "recoverable" — the copy might turn up.
        recoverable_statuses = [
            ItemStatus.AVAILABLE.value,
            ItemStatus.CHECKED_OUT.value,
            ItemStatus.ON_HOLD.value,
            ItemStatus.CLAIMS_RETURNED.value,
        ]
        return (
            self._s.query(Item.id)
            .filter(
                Item.work_id == work_id,
                Item.is_loanable.is_(True),
                Item.status.in_(recoverable_statuses),
            )
            .first()
            is not None
        )

    def availability_counts_for_works(
        self, work_ids: list[int]
    ) -> dict[int, WorkAvailability]:
        """Aggregate copy counts per work for OPAC display.

        available — items with status AVAILABLE (regardless of is_loanable;
                    the count describes physical copies, not loanability)
        total     — items with any status except WITHDRAWN
        status    — 'available' | 'checked_out' (circulating copies exist)
                    | 'unavailable'
        Works with zero items are omitted.
        """
        if not work_ids:
            return {}
        circulating = [
            ItemStatus.CHECKED_OUT.value,
            ItemStatus.ON_HOLD.value,
            ItemStatus.CLAIMS_RETURNED.value,
        ]
        rows = (
            self._s.query(
                Item.work_id,
                func.sum(
                    case((Item.status == ItemStatus.AVAILABLE.value, 1), else_=0)
                ).label("avail"),
                func.sum(
                    case((Item.status != ItemStatus.WITHDRAWN.value, 1), else_=0)
                ).label("total"),
                func.max(
                    case((Item.status.in_(circulating), 1), else_=0)
                ).label("circ"),
            )
            .filter(Item.work_id.in_(work_ids))
            .group_by(Item.work_id)
            .all()
        )
        result: dict[int, WorkAvailability] = {}
        for work_id, avail, total, circ in rows:
            if avail:
                status = "available"
            elif circ:
                status = "checked_out"
            else:
                status = "unavailable"
            result[work_id] = WorkAvailability(int(avail), int(total), status)
        return result

    def first_available_loanable_copy(self, work_id: int) -> Item | None:
        """Pick the earliest-accessioned AVAILABLE loanable copy, if any."""
        return (
            self._s.query(Item)
            .filter(
                Item.work_id == work_id,
                Item.is_loanable.is_(True),
                Item.status == ItemStatus.AVAILABLE.value,
            )
            .order_by(Item.accession_number)
            .first()
        )

    def list(self, limit: int = 50, offset: int = 0) -> list[Work]:
        return self._s.query(Work).order_by(Work.sort_title, Work.title).offset(offset).limit(limit).all()

    def iter_for_export(
        self,
        *,
        media_type_code: str | None = None,
        branch_code: str | None = None,
        since: datetime | None = None,
    ) -> list[Work]:
        q = self._s.query(Work)
        if media_type_code:
            q = q.join(Work.media_type).filter(MediaType.code == media_type_code)
        if branch_code:
            q = (
                q.join(Work.items)
                .join(Item.branch)
                .filter(Branch.code == branch_code)
                .distinct()
            )
        if since is not None:
            q = q.filter(Work.created_at >= since)
        return q.order_by(Work.id).all()

    def iter_for_refresh(
        self,
        *,
        media_type_code: str | None = None,
        branch_code: str | None = None,
        missing_only: bool = True,
        limit: int | None = None,
    ) -> list[Work]:
        # Bulk metadata refresh requires *some* lookup key. v1 covers the
        # common case (post-import Works carry ISBN or UPC); Works whose only
        # key is in external_ids (mbid / tmdb_id) need per-work refresh until
        # we add JSON-portable filtering here.
        q = self._s.query(Work).filter(
            or_(Work.isbn.isnot(None), Work.upc.isnot(None))
        )
        if media_type_code:
            q = q.join(Work.media_type).filter(MediaType.code == media_type_code)
        if branch_code:
            q = (
                q.join(Work.items)
                .join(Item.branch)
                .filter(Branch.code == branch_code)
                .distinct()
            )
        if missing_only:
            # Match the fields _compute_refresh_diff actually fills.
            q = q.filter(
                or_(
                    Work.description.is_(None),
                    Work.description == "",
                    Work.cover_image_url.is_(None),
                    Work.cover_image_url == "",
                    Work.publisher.is_(None),
                    Work.publisher == "",
                    Work.language.is_(None),
                    Work.language == "",
                )
            )
        # Stable ascending order so cron --limit runs make forward progress.
        q = q.order_by(Work.id)
        if limit is not None:
            q = q.limit(limit)
        return q.all()

    def _apply_order(
        self,
        q,
        *,
        order_by: str,
        already_joined_creator: bool = False,
    ):
        if order_by == "recent":
            return q.order_by(Work.created_at.desc(), Work.id.desc())
        if order_by == "author":
            # Outer-join to the primary creator (display_order 0) so works with
            # no creators still appear, sorted last.
            if not already_joined_creator:
                primary_wc = aliased(WorkCreator)
                primary_cr = aliased(Creator)
                q = (
                    q.outerjoin(
                        primary_wc,
                        and_(primary_wc.work_id == Work.id, primary_wc.display_order == 0),
                    )
                    .outerjoin(primary_cr, primary_cr.id == primary_wc.creator_id)
                )
                return q.order_by(primary_cr.sort_name.asc().nullslast(), Work.sort_title)
            # Already joined (author-field search) — order by the joined creator alias.
            return q.order_by(Creator.sort_name.asc().nullslast(), Work.sort_title)
        # "title" / "relevance" (non-FTS path) / unknown — default title order
        return q.order_by(Work.sort_title, Work.title)

    def search(
        self,
        q: str,
        field: str = "all",
        *,
        limit: int = 20,
        offset: int = 0,
        media_type_codes: list[str] | None = None,
        decade: int | None = None,
        available_only: bool = False,
        include_withdrawn_only: bool = False,
        order_by: Literal["title", "author", "recent", "relevance"] = "title",
    ) -> list[Work]:
        # FTS gives us a candidate id-list (already ranked); filters then narrow.
        if field == "all" and q.strip():
            ids = self._fts_ids(q.strip(), limit=10_000)
            if ids is not None:
                return self._post_filter(
                    ids,
                    limit=limit,
                    offset=offset,
                    media_type_codes=media_type_codes,
                    decade=decade,
                    available_only=available_only,
                    include_withdrawn_only=include_withdrawn_only,
                    order_by=order_by,
                )

        base = self._base_filtered(
            media_type_codes=media_type_codes,
            decade=decade,
            available_only=available_only,
            include_withdrawn_only=include_withdrawn_only,
        )
        if not q:
            return (
                self._apply_order(base, order_by=order_by)
                .offset(offset)
                .limit(limit)
                .all()
            )

        pattern = f"%{q}%"
        if field == "title":
            return (
                self._apply_order(base.filter(Work.title.ilike(pattern)), order_by=order_by)
                .offset(offset)
                .limit(limit)
                .all()
            )
        if field == "author":
            return (
                self._apply_order(
                    base.join(Work.creators)
                    .join(WorkCreator.creator)
                    .filter(Creator.display_name.ilike(pattern))
                    .distinct(),
                    order_by=order_by,
                    already_joined_creator=True,
                )
                .offset(offset)
                .limit(limit)
                .all()
            )
        if field == "publisher":
            return (
                self._apply_order(base.filter(Work.publisher.ilike(pattern)), order_by=order_by)
                .offset(offset)
                .limit(limit)
                .all()
            )
        if field == "isbn":
            return (
                self._apply_order(base.filter(Work.isbn.ilike(pattern)), order_by=order_by)
                .offset(offset)
                .limit(limit)
                .all()
            )

        return (
            self._apply_order(
                base.outerjoin(Work.creators)
                .outerjoin(WorkCreator.creator)
                .filter(
                    or_(
                        Work.title.ilike(pattern),
                        Work.publisher.ilike(pattern),
                        Work.isbn.ilike(pattern),
                        Creator.display_name.ilike(pattern),
                    )
                )
                .distinct(),
                order_by=order_by,
            )
            .offset(offset)
            .limit(limit)
            .all()
        )

    def count_search(
        self,
        q: str,
        field: str = "all",
        *,
        media_type_codes: list[str] | None = None,
        decade: int | None = None,
        available_only: bool = False,
        include_withdrawn_only: bool = False,
    ) -> int:
        if field == "all" and q.strip():
            ids = self._fts_ids(q.strip(), limit=10_000)
            if ids is not None:
                return self._count_post_filter(
                    ids,
                    media_type_codes=media_type_codes,
                    decade=decade,
                    available_only=available_only,
                    include_withdrawn_only=include_withdrawn_only,
                )
        # Fall back to the full filter query but counted.
        base = self._base_filtered(
            media_type_codes=media_type_codes,
            decade=decade,
            available_only=available_only,
            include_withdrawn_only=include_withdrawn_only,
        )
        if not q:
            return base.count()
        pattern = f"%{q}%"
        if field == "title":
            return base.filter(Work.title.ilike(pattern)).count()
        if field == "author":
            return (
                base.join(Work.creators)
                .join(WorkCreator.creator)
                .filter(Creator.display_name.ilike(pattern))
                .distinct()
                .count()
            )
        if field == "publisher":
            return base.filter(Work.publisher.ilike(pattern)).count()
        if field == "isbn":
            return base.filter(Work.isbn.ilike(pattern)).count()
        return (
            base.outerjoin(Work.creators)
            .outerjoin(WorkCreator.creator)
            .filter(
                or_(
                    Work.title.ilike(pattern),
                    Work.publisher.ilike(pattern),
                    Work.isbn.ilike(pattern),
                    Creator.display_name.ilike(pattern),
                )
            )
            .distinct()
            .count()
        )

    @staticmethod
    def _active_item_clause():
        """EXISTS clause: at least one non-WITHDRAWN item belongs to the Work."""
        return exists().where(
            (Item.work_id == Work.id)
            & (Item.status != ItemStatus.WITHDRAWN.value)
        )

    def _base_filtered(
        self,
        *,
        media_type_codes: list[str] | None,
        decade: int | None,
        available_only: bool,
        include_withdrawn_only: bool = False,
    ):
        q = self._s.query(Work)
        if not include_withdrawn_only:
            q = q.filter(self._active_item_clause())
        if media_type_codes:
            q = q.join(Work.media_type).filter(MediaType.code.in_(media_type_codes))
        if decade is not None:
            q = q.filter(
                Work.publication_year >= decade,
                Work.publication_year < decade + 10,
            )
        if available_only:
            q = q.filter(
                exists().where(
                    (Item.work_id == Work.id)
                    & Item.is_loanable.is_(True)
                    & (Item.status == ItemStatus.AVAILABLE.value)
                )
            )
        return q

    def _post_filter(
        self,
        ids: list[int],
        *,
        limit: int,
        offset: int,
        media_type_codes: list[str] | None,
        decade: int | None,
        available_only: bool,
        include_withdrawn_only: bool = False,
        order_by: str = "relevance",
    ) -> list[Work]:
        if not ids:
            return []
        q = self._base_filtered(
            media_type_codes=media_type_codes,
            decade=decade,
            available_only=available_only,
            include_withdrawn_only=include_withdrawn_only,
        ).filter(Work.id.in_(ids))
        if order_by == "relevance":
            # Preserve FTS rank order, then paginate.
            works = {w.id: w for w in q.all()}
            ordered = [works[i] for i in ids if i in works]
            return ordered[offset : offset + limit]
        # Non-relevance: re-sort the filtered set by the requested field.
        return (
            self._apply_order(q, order_by=order_by)
            .offset(offset)
            .limit(limit)
            .all()
        )

    def _count_post_filter(
        self,
        ids: list[int],
        *,
        media_type_codes: list[str] | None,
        decade: int | None,
        available_only: bool,
        include_withdrawn_only: bool = False,
    ) -> int:
        if not ids:
            return 0
        return (
            self._base_filtered(
                media_type_codes=media_type_codes,
                decade=decade,
                available_only=available_only,
                include_withdrawn_only=include_withdrawn_only,
            )
            .filter(Work.id.in_(ids))
            .count()
        )

    def facet_media_counts(
        self,
        q: str,
        field: str,
        *,
        decade: int | None = None,
        available_only: bool = False,
        include_withdrawn_only: bool = False,
    ) -> list[tuple[str, str, int]]:
        """Counts grouped by media type for the current search, ignoring any
        media-type selection (so the user can browse alternatives in the group).
        Returns (code, display_name, count)."""
        ids = self._candidate_ids(q, field)
        base = (
            self._s.query(MediaType.code, MediaType.display_name, func.count(Work.id))
            .join(Work, Work.media_type_id == MediaType.id)
        )
        if ids is not None:
            if not ids:
                return []
            base = base.filter(Work.id.in_(ids))
        if not include_withdrawn_only:
            base = base.filter(self._active_item_clause())
        if decade is not None:
            base = base.filter(
                Work.publication_year >= decade,
                Work.publication_year < decade + 10,
            )
        if available_only:
            base = base.filter(
                exists().where(
                    (Item.work_id == Work.id)
                    & Item.is_loanable.is_(True)
                    & (Item.status == ItemStatus.AVAILABLE.value)
                )
            )
        rows = (
            base.group_by(MediaType.code, MediaType.display_name)
            .order_by(MediaType.display_name)
            .all()
        )
        return [(c, n, int(cnt)) for c, n, cnt in rows]

    def facet_decade_counts(
        self,
        q: str,
        field: str,
        *,
        media_type_codes: list[str] | None = None,
        available_only: bool = False,
        include_withdrawn_only: bool = False,
    ) -> list[tuple[int, int]]:
        """Decade buckets with counts (e.g. (2010, 87)). Sorted descending so
        recent years lead. Computed Python-side to avoid sqlite/postgres divides
        differently."""
        ids = self._candidate_ids(q, field)
        base = self._s.query(Work.publication_year).filter(Work.publication_year.isnot(None))
        if ids is not None:
            if not ids:
                return []
            base = base.filter(Work.id.in_(ids))
        if not include_withdrawn_only:
            base = base.filter(self._active_item_clause())
        if media_type_codes:
            base = base.join(Work.media_type).filter(MediaType.code.in_(media_type_codes))
        if available_only:
            base = base.filter(
                exists().where(
                    (Item.work_id == Work.id)
                    & Item.is_loanable.is_(True)
                    & (Item.status == ItemStatus.AVAILABLE.value)
                )
            )
        buckets: dict[int, int] = {}
        for (year,) in base.all():
            buckets[(year // 10) * 10] = buckets.get((year // 10) * 10, 0) + 1
        return sorted(buckets.items(), key=lambda x: x[0], reverse=True)

    def facet_available_count(
        self,
        q: str,
        field: str,
        *,
        media_type_codes: list[str] | None = None,
        decade: int | None = None,
        include_withdrawn_only: bool = False,
    ) -> int:
        ids = self._candidate_ids(q, field)
        base = self._base_filtered(
            media_type_codes=media_type_codes,
            decade=decade,
            available_only=True,
            include_withdrawn_only=include_withdrawn_only,
        )
        if ids is not None:
            if not ids:
                return 0
            base = base.filter(Work.id.in_(ids))
        return base.count()

    def _candidate_ids(self, q: str, field: str) -> list[int] | None:
        """Resolve the search q+field to a candidate id list. Returns None when
        no narrowing is needed (empty query or non-FTS fields handled by joins)."""
        if not q or not q.strip():
            return None
        if field == "all":
            return self._fts_ids(q.strip(), limit=10_000) or []
        pattern = f"%{q}%"
        base = self._s.query(Work.id)
        if field == "title":
            rows = base.filter(Work.title.ilike(pattern)).all()
        elif field == "author":
            rows = (
                base.join(Work.creators)
                .join(WorkCreator.creator)
                .filter(Creator.display_name.ilike(pattern))
                .distinct()
                .all()
            )
        elif field == "publisher":
            rows = base.filter(Work.publisher.ilike(pattern)).all()
        elif field == "isbn":
            rows = base.filter(Work.isbn.ilike(pattern)).all()
        else:
            rows = (
                base.outerjoin(Work.creators)
                .outerjoin(WorkCreator.creator)
                .filter(
                    or_(
                        Work.title.ilike(pattern),
                        Work.publisher.ilike(pattern),
                        Work.isbn.ilike(pattern),
                        Creator.display_name.ilike(pattern),
                    )
                )
                .distinct()
                .all()
            )
        return [r[0] for r in rows]

    def list_recent(self, *, days: int, limit: int, include_withdrawn_only: bool = False) -> list[Work]:
        from datetime import timedelta

        cutoff = datetime.now(tz=UTC) - timedelta(days=days)
        q = (
            self._s.query(Work)
            .options(
                selectinload(Work.creators).selectinload(WorkCreator.creator)
            )
            .filter(Work.created_at >= cutoff)
        )
        if not include_withdrawn_only:
            q = q.filter(self._active_item_clause())
        return q.order_by(Work.created_at.desc()).limit(limit).all()

    def list_recently_returned(self, *, days: int, limit: int, include_withdrawn_only: bool = False) -> list[Work]:
        from datetime import timedelta

        cutoff = datetime.now(tz=UTC) - timedelta(days=days)
        # Window: works whose most recent loan was returned in the last `days`.
        sub = (
            self._s.query(Item.work_id, func.max(Loan.returned_at).label("last"))
            .join(Loan, Loan.item_id == Item.id)
            .filter(Loan.returned_at.isnot(None))
            .group_by(Item.work_id)
            .subquery()
        )
        q = (
            self._s.query(Work, sub.c.last)
            .options(
                selectinload(Work.creators).selectinload(WorkCreator.creator)
            )
            .join(sub, sub.c.work_id == Work.id)
            .filter(sub.c.last >= cutoff)
        )
        if not include_withdrawn_only:
            q = q.filter(self._active_item_clause())
        rows = q.order_by(sub.c.last.desc()).limit(limit).all()
        return [w for w, _last in rows]

    def _fts_ids(self, q: str, *, limit: int) -> list[int] | None:
        """Return ranked work IDs matching `q` via FTS, or None on unsupported backend."""
        dialect = self._s.connection().dialect.name
        if dialect == "sqlite":
            fts_query = '"' + q.replace('"', '""') + '"'
            rows = self._s.execute(
                text(
                    "SELECT rowid FROM work_fts WHERE work_fts MATCH :q ORDER BY rank LIMIT :lim"
                ),
                {"q": fts_query, "lim": limit},
            ).fetchall()
            return [r[0] for r in rows]
        if dialect == "postgresql":
            rows = self._s.execute(
                text(
                    """
                    SELECT id
                    FROM work
                    WHERE to_tsvector('english', COALESCE(search_text, ''))
                          @@ plainto_tsquery('english', :q)
                    ORDER BY ts_rank(
                        to_tsvector('english', COALESCE(search_text, '')),
                        plainto_tsquery('english', :q)
                    ) DESC
                    LIMIT :lim
                    """
                ),
                {"q": q, "lim": limit},
            ).fetchall()
            return [r[0] for r in rows]
        return None

    def suggest(self, q: str, *, limit: int = 8) -> list[Work]:
        if len(q.strip()) < 2:
            return []
        tokens = [_TOKEN_SANITIZE.sub("", t) for t in q.split()]
        tokens = [t for t in tokens if t]
        if not tokens:
            return []
        ids = self._fts_prefix_ids(tokens, limit)
        if not ids:
            return []
        id_order = {work_id: pos for pos, work_id in enumerate(ids)}
        works = (
            self._s.query(Work)
            .options(selectinload(Work.creators).selectinload(WorkCreator.creator))
            .filter(Work.id.in_(ids))
            .all()
        )
        works.sort(key=lambda w: id_order.get(w.id, len(ids)))
        return works

    def _fts_prefix_ids(self, tokens: list[str], limit: int) -> list[int]:
        dialect = self._s.connection().dialect.name
        if dialect == "sqlite":
            expr = " ".join(f"{t}*" for t in tokens)
            rows = self._s.execute(
                text(
                    "SELECT rowid FROM work_fts WHERE work_fts MATCH :q ORDER BY rank LIMIT :lim"
                ),
                {"q": expr, "lim": limit},
            ).fetchall()
            return [r[0] for r in rows]
        if dialect == "postgresql":
            expr = " & ".join(f"{t}:*" for t in tokens)
            rows = self._s.execute(
                text(
                    """
                    SELECT id FROM work
                    WHERE to_tsvector('english', COALESCE(search_text, ''))
                          @@ to_tsquery('english', :q)
                    ORDER BY id DESC
                    LIMIT :lim
                    """
                ),
                {"q": expr, "lim": limit},
            ).fetchall()
            return [r[0] for r in rows]
        return []
