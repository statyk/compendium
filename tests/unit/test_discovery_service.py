"""Unit tests for DiscoveryService."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from compendium.services.discovery import DiscoveryService


def _svc(work_repo=None):
    return DiscoveryService(work_repo=work_repo or MagicMock())


class TestSearchPagination:
    def test_offset_computed_from_page(self):
        repo = MagicMock()
        repo.search.return_value = []
        repo.count_search.return_value = 0
        repo.facet_media_counts.return_value = []
        repo.facet_decade_counts.return_value = []
        repo.facet_available_count.return_value = 0
        svc = _svc(repo)

        svc.search("dune", page=3, page_size=25)

        assert repo.search.call_args.kwargs["offset"] == 50
        assert repo.search.call_args.kwargs["limit"] == 25

    def test_page_below_one_clamped(self):
        repo = MagicMock()
        repo.search.return_value = []
        repo.count_search.return_value = 0
        repo.facet_media_counts.return_value = []
        repo.facet_decade_counts.return_value = []
        repo.facet_available_count.return_value = 0
        svc = _svc(repo)

        svc.search("dune", page=-5)

        assert repo.search.call_args.kwargs["offset"] == 0


class TestFacetIsolation:
    """Each facet count should be computed without that facet's own selection,
    so users can browse alternatives within the group."""

    def test_media_count_drops_media_filter(self):
        repo = MagicMock()
        svc = _svc(repo)
        svc.facet_counts(
            "dune", media_type_codes=["book"], decade=2010, available_only=True
        )
        # facet_media_counts should NOT receive media_type_codes (the group it represents)
        # but SHOULD receive decade and available_only.
        kwargs = repo.facet_media_counts.call_args.kwargs
        assert "media_type_codes" not in kwargs
        assert kwargs["decade"] == 2010
        assert kwargs["available_only"] is True

    def test_decade_count_drops_decade_filter(self):
        repo = MagicMock()
        svc = _svc(repo)
        svc.facet_counts(
            "dune", media_type_codes=["book"], decade=2010, available_only=True
        )
        kwargs = repo.facet_decade_counts.call_args.kwargs
        assert "decade" not in kwargs
        assert kwargs["media_type_codes"] == ["book"]
        assert kwargs["available_only"] is True

    def test_available_count_drops_available_filter(self):
        repo = MagicMock()
        svc = _svc(repo)
        svc.facet_counts(
            "dune", media_type_codes=["book"], decade=2010, available_only=True
        )
        kwargs = repo.facet_available_count.call_args.kwargs
        assert "available_only" not in kwargs
        assert kwargs["media_type_codes"] == ["book"]
        assert kwargs["decade"] == 2010


class TestPageHelpers:
    def test_has_prev_and_next(self):
        repo = MagicMock()
        repo.search.return_value = [SimpleNamespace(id=i) for i in range(25)]
        repo.count_search.return_value = 80
        repo.facet_media_counts.return_value = []
        repo.facet_decade_counts.return_value = []
        repo.facet_available_count.return_value = 0
        svc = _svc(repo)

        page = svc.search("", page=2, page_size=25)
        assert page.has_prev is True
        assert page.has_next is True

        page = svc.search("", page=4, page_size=25)
        assert page.has_prev is True
        assert page.has_next is False
