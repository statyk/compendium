from compendium.domain.models import MediaType, Work
from compendium.repositories.sql.work_repository import SqlWorkRepository


def _make_work(session, external_ids):
    mt = session.query(MediaType).filter_by(code="vinyl").first()
    w = Work(title="T", sort_title="t", media_type_id=mt.id, external_ids=external_ids)
    session.add(w)
    session.flush()
    return w


def test_get_by_external_id_match(session):
    _make_work(session, {"discogs": "12345"})
    repo = SqlWorkRepository(session)
    found = repo.get_by_external_id("discogs", "12345")
    assert found is not None and found.external_ids["discogs"] == "12345"


def test_get_by_external_id_miss(session):
    _make_work(session, {"discogs": "12345"})
    repo = SqlWorkRepository(session)
    assert repo.get_by_external_id("discogs", "99999") is None
    assert repo.get_by_external_id("musicbrainz", "12345") is None


def test_get_by_external_id_match_postgres(pg_session):
    _make_work(pg_session, {"discogs": "77"})
    repo = SqlWorkRepository(pg_session)
    assert repo.get_by_external_id("discogs", "77") is not None
    assert repo.get_by_external_id("discogs", "78") is None
