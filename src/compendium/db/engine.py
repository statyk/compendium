from functools import lru_cache

from sqlalchemy import Engine, create_engine

from compendium.config.settings import Settings


def make_engine(settings: Settings) -> Engine:
    kwargs: dict = {}
    if settings.database_url.startswith("sqlite"):
        # SQLite needs this for multi-threaded CLI use
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(settings.database_url, **kwargs)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings())
