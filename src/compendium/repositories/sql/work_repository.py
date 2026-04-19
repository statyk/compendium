from __future__ import annotations

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from compendium.domain.models import Creator, Work, WorkCreator


class SqlWorkRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, work: Work) -> Work:
        self._s.add(work)
        self._s.flush()
        return work

    def get(self, id: int) -> Work | None:
        return self._s.get(Work, id)

    def get_by_isbn(self, isbn: str) -> Work | None:
        return self._s.query(Work).filter_by(isbn=isbn).first()

    def get_by_upc(self, upc: str) -> Work | None:
        return self._s.query(Work).filter_by(upc=upc).first()

    def list(self, limit: int = 50, offset: int = 0) -> list[Work]:
        return self._s.query(Work).order_by(Work.title).offset(offset).limit(limit).all()

    def search(self, q: str, field: str = "all", limit: int = 20) -> list[Work]:
        if field == "all" and q.strip():
            results = self._fts_search(q.strip(), limit)
            if results is not None:
                return results

        pattern = f"%{q}%"
        base = self._s.query(Work)

        if field == "title":
            return base.filter(Work.title.ilike(pattern)).order_by(Work.title).limit(limit).all()
        if field == "author":
            return (
                base.join(Work.creators)
                .join(WorkCreator.creator)
                .filter(Creator.display_name.ilike(pattern))
                .order_by(Work.title)
                .distinct()
                .limit(limit)
                .all()
            )
        if field == "publisher":
            return (
                base.filter(Work.publisher.ilike(pattern)).order_by(Work.title).limit(limit).all()
            )
        if field == "isbn":
            return base.filter(Work.isbn.ilike(pattern)).order_by(Work.title).limit(limit).all()

        # default all-fields LIKE fallback
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
            .order_by(Work.title)
            .distinct()
            .limit(limit)
            .all()
        )

    def _fts_search(self, q: str, limit: int) -> list[Work] | None:
        dialect = self._s.connection().dialect.name
        if dialect == "sqlite":
            return self._fts_sqlite(q, limit)
        if dialect == "postgresql":
            return self._fts_postgres(q, limit)
        return None

    def _fts_sqlite(self, q: str, limit: int) -> list[Work]:
        # Wrap in FTS5 phrase quotes so special chars (., -, *, etc.) are literals.
        fts_query = '"' + q.replace('"', '""') + '"'
        rows = self._s.execute(
            text(
                "SELECT rowid FROM work_fts WHERE work_fts MATCH :q ORDER BY rank LIMIT :lim"
            ),
            {"q": fts_query, "lim": limit},
        ).fetchall()
        ids = [r[0] for r in rows]
        if not ids:
            return []
        works = {w.id: w for w in self._s.query(Work).filter(Work.id.in_(ids)).all()}
        return [works[i] for i in ids if i in works]

    def _fts_postgres(self, q: str, limit: int) -> list[Work]:
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
        ids = [r[0] for r in rows]
        if not ids:
            return []
        works = {w.id: w for w in self._s.query(Work).filter(Work.id.in_(ids)).all()}
        return [works[i] for i in ids if i in works]
