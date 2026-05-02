"""Integration tests for FTS search (SQLite FTS5)."""
from unittest.mock import patch

import pytest

from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.services.catalog import CatalogService

_OPEN_LIB_DUNE = {
    "title": "Dune",
    "authors": [{"name": "Frank Herbert"}],
    "publishers": [{"name": "Chilton Books"}],
    "publish_date": "1965",
    "cover": {},
    "identifiers": {},
}

_OPEN_LIB_FOUNDATION = {
    "title": "Foundation",
    "authors": [{"name": "Isaac Asimov"}],
    "publishers": [{"name": "Gnome Press"}],
    "publish_date": "1951",
    "cover": {},
    "identifiers": {},
}

_ISBN_DUNE = "9780441013593"
_ISBN_FOUNDATION = "9780553293357"


def _catalog(session) -> CatalogService:
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
    )


@pytest.fixture
def two_books(session):
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        _catalog(session).add_from_isbn(_ISBN_DUNE)
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_FOUNDATION):
        _catalog(session).add_from_isbn(_ISBN_FOUNDATION)
    session.flush()


def test_fts_finds_by_title(session, two_books):
    results = SqlWorkRepository(session).search("Dune")
    titles = [w.title for w in results]
    assert "Dune" in titles
    assert "Foundation" not in titles


def test_fts_finds_by_author(session, two_books):
    results = SqlWorkRepository(session).search("Asimov")
    titles = [w.title for w in results]
    assert "Foundation" in titles
    assert "Dune" not in titles


def test_fts_returns_empty_for_no_match(session, two_books):
    results = SqlWorkRepository(session).search("xyznonexistent")
    assert results == []


def test_fts_field_title_still_works(session, two_books):
    results = SqlWorkRepository(session).search("Dune", field="title")
    assert any(w.title == "Dune" for w in results)


def test_fts_tolerates_special_characters(session, two_books):
    # FTS5 treats '.', '-', '*' etc. as syntax; they must not raise a 500.
    for q in ["thishasa.", "hello-world", "foo*", "a.b.c", "(parens)"]:
        results = SqlWorkRepository(session).search(q)
        assert isinstance(results, list)


def test_search_text_populated_on_work(session):
    with patch("compendium.services.metadata.lookup_isbn", return_value=_OPEN_LIB_DUNE):
        work, _ = _catalog(session).add_from_isbn("9780441013594")
    session.refresh(work)
    assert work.search_text is not None
    assert "Dune" in work.search_text
    assert "Frank Herbert" in work.search_text


# ── Suggest tests ─────────────────────────────────────────────────────────────


def _add_work(session, title: str, author: str | None = None):
    from compendium.domain.models import MediaType, Work

    mt = session.query(MediaType).filter_by(code="book").first()
    search = f"{title} {author}" if author else title
    w = Work(title=title, search_text=search, media_type_id=mt.id)
    session.add(w)
    session.flush()
    return w


def test_suggest_returns_prefix_matches(session):
    _add_work(session, "Foundation", "Isaac Asimov")
    _add_work(session, "Foundation and Empire", "Isaac Asimov")
    results = SqlWorkRepository(session).suggest("found")
    titles = [w.title for w in results]
    assert "Foundation" in titles
    assert "Foundation and Empire" in titles


def test_suggest_short_query_returns_empty(session):
    assert SqlWorkRepository(session).suggest("a") == []


def test_suggest_empty_query_returns_empty(session):
    assert SqlWorkRepository(session).suggest("") == []


def test_suggest_strips_punctuation(session):
    _add_work(session, "Foundation", "Isaac Asimov")
    repo = SqlWorkRepository(session)
    clean = repo.suggest("found")
    punct = repo.suggest("found?")
    assert [w.title for w in clean] == [w.title for w in punct]


def test_suggest_multi_token_anded(session):
    _add_work(session, "Foundation", "Isaac Asimov")
    _add_work(session, "Dune", "Frank Herbert")
    repo = SqlWorkRepository(session)
    # "found dune" requires both prefix tokens in the same work — no match.
    multi = repo.suggest("found dune")
    assert not any(w.title == "Foundation" for w in multi)
    # Single-token prefix finds Foundation.
    single = repo.suggest("foundation")
    assert any(w.title == "Foundation" for w in single)


def test_suggest_respects_limit(session):
    from compendium.domain.models import MediaType, Work

    mt = session.query(MediaType).filter_by(code="book").first()
    for i in range(12):
        w = Work(title=f"Foobar Book {i}", search_text=f"foobar book {i}", media_type_id=mt.id)
        session.add(w)
    session.flush()
    results = SqlWorkRepository(session).suggest("foobar", limit=8)
    assert len(results) == 8
