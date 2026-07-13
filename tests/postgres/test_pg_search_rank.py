"""ts_rank relevance ordering on the Postgres backend (UX slice 7)."""
from compendium.domain.models import MediaType, Work
from compendium.repositories.sql.work_repository import SqlWorkRepository


def test_relevance_rank_differs_from_title_order_pg(session):
    mt = session.query(MediaType).filter_by(code="book").first()
    session.add(Work(title="Aardvark and the Dune", sort_title="aardvark and the dune",
                     search_text="Aardvark and the Dune", media_type_id=mt.id))
    session.add(Work(title="Zebra Atlas", sort_title="zebra atlas",
                     search_text="Zebra Atlas Dune Dune Dune Dune Dune", media_type_id=mt.id))
    session.flush()

    repo = SqlWorkRepository(session)
    by_rank = [w.title for w in repo.search("Dune", order_by="relevance", include_withdrawn_only=True)]
    assert by_rank.index("Zebra Atlas") < by_rank.index("Aardvark and the Dune")
