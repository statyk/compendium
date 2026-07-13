"""Filter/search behavior of SqlPatronRepository.list/count (UX slice 2)."""
import pytest

from compendium.domain.models import Patron
from compendium.repositories.sql.patron_repository import SqlPatronRepository


def _add(session, card, name, email=None, active=True):
    p = Patron(
        library_card_number=card,
        full_name=name,
        contact_email=email,
        is_active=active,
    )
    session.add(p)
    session.flush()
    return p


class TestPatronRepositoryFilters:
    def test_query_matches_name_card_and_email(self, session):
        repo = SqlPatronRepository(session)
        _add(session, "RC0001", "Ada Lovelace", "ada@calc.org")
        _add(session, "RC0002", "Charles Babbage", "chas@engine.org")

        assert [p.full_name for p in repo.list(query="lovel")] == ["Ada Lovelace"]
        assert [p.full_name for p in repo.list(query="rc0002")] == ["Charles Babbage"]
        assert [p.full_name for p in repo.list(query="@calc")] == ["Ada Lovelace"]
        assert repo.list(query="no-such-patron") == []

    def test_status_filters_and_count(self, session):
        repo = SqlPatronRepository(session)
        _add(session, "RS0001", "Active Alice")
        _add(session, "RS0002", "Idle Ivan", active=False)

        assert [p.full_name for p in repo.list(status="active", query="RS000")] == ["Active Alice"]
        assert [p.full_name for p in repo.list(status="inactive", query="RS000")] == ["Idle Ivan"]
        assert {p.full_name for p in repo.list(status="all", query="RS000")} == {
            "Active Alice",
            "Idle Ivan",
        }
        assert repo.count(status="all", query="RS000") == 2
        assert repo.count(status="active", query="RS000") == 1
        assert repo.count(status="inactive", query="RS000") == 1

    def test_ordering_and_offset(self, session):
        repo = SqlPatronRepository(session)
        _add(session, "RO0001", "Ord Bravo")
        _add(session, "RO0002", "Ord Alpha")

        names = [p.full_name for p in repo.list(query="RO000")]
        assert names == ["Ord Alpha", "Ord Bravo"]  # ordered by full_name
        assert [p.full_name for p in repo.list(query="RO000", limit=1, offset=1)] == ["Ord Bravo"]

    def test_unknown_status_rejected(self, session):
        with pytest.raises(ValueError):
            SqlPatronRepository(session).list(status="bogus")
