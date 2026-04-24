"""Site-settings read helper with env → DB → default precedence + cache.

Callers read settings via ``get_site_setting(key)``. The helper:

1. Checks the corresponding environment variable. If set, it's parsed
   strictly — an unparseable env var raises ``SettingValidationError``
   (matches Pydantic's fail-loud behavior).
2. Falls back to the ``site_setting`` table, using a process-local cache
   keyed on ``MAX(updated_at)`` so reloads only happen when a row actually
   changed.
3. Falls back to the descriptor's default.

Writes go through ``set_site_setting()`` which bumps the cache. Callers
that mutate rows through other paths (tests, backup restore) must call
``invalidate_cache()`` afterwards.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from compendium.domain.models import SiteSetting
from compendium.repositories.sql.site_setting_repository import SqlSiteSettingRepository
from compendium.services.settings_registry import (
    SettingsRegistryError,
    encode_for_storage,
    get_descriptor,
    parse,
    validate,
)


_log = logging.getLogger("compendium")

# How long to trust the cache before checking MAX(updated_at). Short enough
# that multi-worker deployments see writes promptly, long enough to avoid
# a per-read DB round trip. Not itself a site_setting — chicken/egg.
_CACHE_TTL_SECONDS = 30.0

_lock = threading.Lock()
_cache: dict[str, Any] = {}
_cache_epoch: datetime | None = None
_last_check: float = 0.0


def invalidate_cache() -> None:
    """Wipe the cache so the next read rehits the DB.

    Callers: ``set_site_setting()`` (automatic), backup-restore (post-wipe),
    tests that mutate ``site_setting`` out-of-band.
    """
    global _cache, _cache_epoch, _last_check
    with _lock:
        _cache = {}
        _cache_epoch = None
        _last_check = 0.0


def _get_engine_lazy() -> Engine:
    # Imported lazily so tests patching compendium.db.engine.get_engine
    # always see the live reference.
    from compendium.db.engine import get_engine

    return get_engine()


def _refresh_cache_if_needed() -> None:
    global _cache, _cache_epoch, _last_check
    now = time.monotonic()
    if now - _last_check < _CACHE_TTL_SECONDS and _cache_epoch is not None:
        return
    with _lock:
        # Double-check under the lock
        now = time.monotonic()
        if now - _last_check < _CACHE_TTL_SECONDS and _cache_epoch is not None:
            return
        engine = _get_engine_lazy()
        try:
            with Session(engine) as session:
                repo = SqlSiteSettingRepository(session)
                max_updated = repo.max_updated_at()
                if max_updated == _cache_epoch and _cache_epoch is not None:
                    _last_check = now
                    return
                rows = repo.all()
                _cache = {row.key: row.value for row in rows}
                _cache_epoch = max_updated or datetime.min
                _last_check = now
        except (OperationalError, ProgrammingError) as exc:
            # site_setting table missing (pre-db-init, test engine without
            # the new schema, etc). Return defaults rather than hard-failing
            # a caller that just wanted to read `library_name`.
            msg = str(exc).lower()
            if "site_setting" in msg and (
                "no such table" in msg or "does not exist" in msg
            ):
                _log.warning(
                    "site_setting table not available; using defaults. "
                    "Run 'compendium db init' or 'compendium db upgrade'."
                )
                _cache = {}
                _cache_epoch = datetime.min
                _last_check = now
                return
            raise


def get_site_setting(key: str) -> Any:
    """Return the current value of a registered setting."""
    desc = get_descriptor(key)

    env_var = desc.resolved_env_var()
    raw_env = os.environ.get(env_var)
    if raw_env is not None:
        # Fail loud: a misconfigured env var should not silently fall through.
        return parse(desc, raw_env)

    _refresh_cache_if_needed()
    raw = _cache.get(key)
    if raw is None:
        return desc.default
    try:
        return parse(desc, raw)
    except SettingsRegistryError:
        _log.exception(
            "site_setting row %r has unparseable value; using default", key
        )
        return desc.default


def set_site_setting(
    key: str,
    value: Any,
    *,
    session: Session,
    updated_by_id: int | None = None,
) -> SiteSetting:
    """Validate, persist, and invalidate-cache for a setting key.

    The session is caller-owned; this does not commit. Callers typically
    commit after calling, which is fine — cache invalidation runs now
    and the next read will re-query (seeing the committed row because
    the caller commits before returning).
    """
    desc = get_descriptor(key)
    validate(desc, value)
    raw = encode_for_storage(value, desc.type)
    repo = SqlSiteSettingRepository(session)
    row = repo.upsert(key, raw, updated_by_id=updated_by_id)
    invalidate_cache()
    return row


def delete_site_setting(key: str, *, session: Session) -> bool:
    """Remove a setting override; subsequent reads fall through to default."""
    get_descriptor(key)  # ensure registered — prevents typos
    repo = SqlSiteSettingRepository(session)
    result = repo.delete(key)
    invalidate_cache()
    return result
