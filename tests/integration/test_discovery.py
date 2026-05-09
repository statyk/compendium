"""Integration tests for DiscoveryService against SQLite (real repos + FTS)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from compendium.domain.enums import ItemStatus
from compendium.domain.models import Item, Loan, MediaType, Patron, Work
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.discovery import DiscoveryService

_counter = {"n": 0}


def _next() -> int:
    _counter["n"] += 1
    return _counter["n"]


def _add_work(session, *, title, media_code="book", year=None, search_text=None):
    n = _next()
    mt = session.query(MediaType).filter_by(code=media_code).one()
    w = Work(
        title=title,
        media_type_id=mt.id,
        publication_year=year,
        search_text=search_text or title,
    )
    session.add(w)
    session.flush()
    return w


def _add_item(session, work, *, status=ItemStatus.AVAILABLE.value, loanable=True):
    n = _next()
    branch = SqlBranchRepository(session).get_default()
    it = Item(
        work_id=work.id,
        branch_id=branch.id,
        barcode=f"DBC{n:06d}",
        accession_number=f"DACC{n:06d}",
        status=status,
        is_loanable=loanable,
    )
    session.add(it)
    session.flush()
    return it


def _patron(session, card):
    p = Patron(library_card_number=card, full_name="Alice")
    session.add(p)
    session.flush()
    return p


def _loan_returned(session, item, patron, *, returned_at):
    branch = SqlBranchRepository(session).get_default()
    ln = Loan(
        item_id=item.id,
        patron_id=patron.id,
        branch_id=branch.id,
        checked_out_at=returned_at - timedelta(days=7),
        due_at=returned_at + timedelta(days=7),
        returned_at=returned_at,
    )
    session.add(ln)
    session.flush()
    return ln


def _svc(session):
    return DiscoveryService(work_repo=SqlWorkRepository(session))


class TestSearchFilters:
    def test_filter_by_media_type(self, session):
        b = _add_work(session, title="Book Alpha", media_code="book")
        d = _add_work(session, title="DVD Alpha", media_code="dvd")
        _add_item(session, b)
        _add_item(session, d)

        page = _svc(session).search("Alpha", media_type_codes=["dvd"])
        assert [w.id for w in page.works] == [d.id]

    def test_filter_by_decade(self, session):
        old = _add_work(session, title="DecadeFilter Old", year=1995)
        new = _add_work(session, title="DecadeFilter New", year=2015)
        _add_item(session, old)
        _add_item(session, new)

        page = _svc(session).search("DecadeFilter", decade=2010)
        ids = {w.id for w in page.works}
        assert new.id in ids
        assert old.id not in ids

    def test_available_only_excludes_checked_out(self, session):
        w = _add_work(session, title="OnlyCopy")
        _add_item(session, w, status=ItemStatus.CHECKED_OUT.value)

        # Without filter, the work is in results.
        all_page = _svc(session).search("OnlyCopy")
        assert w.id in {x.id for x in all_page.works}

        # With available_only, it's excluded (no AVAILABLE copy).
        avail_page = _svc(session).search("OnlyCopy", available_only=True)
        assert w.id not in {x.id for x in avail_page.works}


class TestPagination:
    def test_pages_through_results(self, session):
        # Five works matching "PageTest" — field="title" uses substring,
        # since FTS5 tokenizes on word boundaries.
        works = [_add_work(session, title=f"PageTest {i}") for i in range(5)]
        for w in works:
            _add_item(session, w)
        ids = [w.id for w in works]

        p1 = _svc(session).search("PageTest", field="title", page=1, page_size=2)
        p2 = _svc(session).search("PageTest", field="title", page=2, page_size=2)
        p3 = _svc(session).search("PageTest", field="title", page=3, page_size=2)

        assert p1.total == 5
        assert len(p1.works) == 2
        assert len(p2.works) == 2
        assert len(p3.works) == 1
        # All five appear across pages, no overlap
        seen = {w.id for w in p1.works} | {w.id for w in p2.works} | {w.id for w in p3.works}
        assert seen == set(ids)


class TestFacetCounts:
    def test_media_counts_reflect_other_filters(self, session):
        # Two books in 2010s, one DVD in 2010s, one book in 1990s.
        # Use field="title" to lean on substring match instead of FTS tokens.
        for title, media, year in [
            ("MFAtest one", "book", 2010),
            ("MFAtest two", "book", 2015),
            ("MFAtest three", "dvd", 2012),
            ("MFAtest four", "book", 1995),
        ]:
            _add_item(session, _add_work(session, title=title, media_code=media, year=year))

        facets = _svc(session).facet_counts("MFAtest", field="title", decade=2010)
        counts = {code: n for code, _name, n in facets.media_type}
        assert counts.get("book") == 2
        assert counts.get("dvd") == 1

    def test_decade_counts_drop_decade_filter(self, session):
        _add_item(session, _add_work(session, title="DTtest old", year=1995))
        _add_item(session, _add_work(session, title="DTtest new", year=2015))

        facets = _svc(session).facet_counts("DTtest", field="title", decade=2010)
        decades = {d for d, _ in facets.decade}
        assert 1990 in decades
        assert 2010 in decades

    def test_available_count_independent_of_avail_filter(self, session):
        w1 = _add_work(session, title="ACtest one")
        _add_item(session, w1, status=ItemStatus.AVAILABLE.value)
        w2 = _add_work(session, title="ACtest two")
        _add_item(session, w2, status=ItemStatus.CHECKED_OUT.value)

        facets = _svc(session).facet_counts("ACtest", field="title", available_only=True)
        assert facets.available_now == 1


class TestNewArrivals:
    def test_returns_recent_works_in_order(self, session):
        old = _add_work(session, title="OldTitle")
        new = _add_work(session, title="NewTitle")
        _add_item(session, old)
        _add_item(session, new)
        # Force created_at: SQLAlchemy server_default uses NOW(), so override on session.
        session.execute(
            Work.__table__.update().where(Work.id == old.id).values(
                created_at=datetime.now(tz=timezone.utc) - timedelta(days=120)
            )
        )
        session.execute(
            Work.__table__.update().where(Work.id == new.id).values(
                created_at=datetime.now(tz=timezone.utc) - timedelta(days=2)
            )
        )
        session.flush()
        works = _svc(session).new_arrivals(days=60, limit=10)
        ids = [w.id for w in works]
        assert new.id in ids
        assert old.id not in ids


class TestRecentlyReturned:
    def test_orders_by_most_recent_return(self, session):
        wa = _add_work(session, title="ReturnA")
        wb = _add_work(session, title="ReturnB")
        ia = _add_item(session, wa)
        ib = _add_item(session, wb)
        p = _patron(session, "RR0001")
        now = datetime.now(tz=timezone.utc)
        _loan_returned(session, ia, p, returned_at=now - timedelta(days=10))
        _loan_returned(session, ib, p, returned_at=now - timedelta(days=2))

        works = _svc(session).recently_returned(days=30, limit=10)
        # Most recent first
        assert [w.id for w in works[:2]] == [wb.id, wa.id]

    def test_excludes_old_returns_outside_window(self, session):
        w = _add_work(session, title="ReturnOld")
        i = _add_item(session, w)
        p = _patron(session, "RR0002")
        _loan_returned(
            session, i, p, returned_at=datetime.now(tz=timezone.utc) - timedelta(days=100)
        )
        works = _svc(session).recently_returned(days=30, limit=10)
        assert w.id not in {x.id for x in works}
