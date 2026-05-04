"""Integration tests for SqlCounterRepository."""

import pytest

from compendium.repositories.sql.counters import SqlCounterRepository


def test_next_returns_incrementing_values(session):
    repo = SqlCounterRepository(session)
    assert repo.next("test.counter") == 1
    assert repo.next("test.counter") == 2
    assert repo.next("test.counter") == 3


def test_next_seeds_at_zero_on_first_call(session):
    repo = SqlCounterRepository(session)
    first = repo.next("fresh.key")
    assert first == 1


def test_independent_keys_do_not_interfere(session):
    repo = SqlCounterRepository(session)
    repo.next("key.a")
    repo.next("key.a")
    repo.next("key.b")
    assert repo.next("key.a") == 3
    assert repo.next("key.b") == 2


def test_catalog_accession_key(session):
    """The key used by CatalogService works correctly."""
    repo = SqlCounterRepository(session)
    val = repo.next("catalog.accession")
    assert isinstance(val, int)
    assert val >= 1
