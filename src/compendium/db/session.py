from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy.orm import Session, sessionmaker

from compendium.db.engine import get_engine


def _make_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session; commit on success, roll back on error.

    When a host engine/scope has been installed via
    ``compendium.db.engine.bind()``, delegates to the bound scope so that
    all Compendium-internal DB access targets the host's database.
    """
    from compendium.db.engine import bound_session_scope
    bound = bound_session_scope()
    if bound is not None:
        with bound() as session:
            yield session
        return
    factory = _make_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a committed-or-rolled-back session."""
    factory = _make_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
