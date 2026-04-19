"""Override the `session` fixture for the postgres/ subtree to use pg_session."""
import pytest


@pytest.fixture
def session(pg_session):
    return pg_session
