"""Per-identity login rate limiting (M1, 2026-04-27 security audit).

Sliding-window throttle backed by the failed_login table so it coordinates
across multiple worker processes without a Redis dependency.

Scopes
------
- ``login_user``  — username on /auth/login and /ui/login
- ``kiosk_card``  — library card number on /ui/kiosk/start

Design notes
------------
- Strictly identity-keyed (no IP keying).  Behind a reverse proxy
  request.client.host is the proxy's IP, shared by all users; an IP-keyed
  throttle would become a site-wide lockout.  Credential-stuffing protection
  is delegated to the edge tier (nginx limit_req / Caddy rate_limit).
- Sliding window: once count >= max_failures in the last window_seconds,
  the next attempt is blocked.  New failures inside the window do NOT extend
  the lockout — the window advances naturally so the oldest failure ages out.
- clear() is called on successful login so a user who fat-fingered their
  password many times doesn't carry stale failures into the next visit.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from compendium.repositories.sql.failed_login_repository import SqlFailedLoginRepository
from compendium.services.site_settings import get_site_setting


class RateLimitService:
    def __init__(self, repo: SqlFailedLoginRepository) -> None:
        self._repo = repo

    def _policy(self) -> tuple[int, int]:
        """Return (max_failures, window_seconds) from site settings."""
        return (
            int(get_site_setting("login_max_failures")),
            int(get_site_setting("login_failure_window_seconds")),
        )

    def check(self, scope: str, identifier: str) -> int | None:
        """Return seconds until next attempt is allowed, or None if not blocked.

        A return value > 0 means the caller should reply with 429 and set a
        Retry-After header to this value.  Returns None when max_failures == 0
        (throttling disabled).
        """
        max_failures, window_seconds = self._policy()
        if max_failures == 0:
            return None
        now = datetime.now(timezone.utc)
        since = now - timedelta(seconds=window_seconds)
        count, oldest = self._repo.count_and_oldest(scope, identifier, since)
        if count < max_failures or oldest is None:
            return None
        # Sliding window: how many seconds until the oldest failure ages out.
        oldest_aware = oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)
        retry_after = math.ceil(
            (oldest_aware + timedelta(seconds=window_seconds) - now).total_seconds()
        )
        return max(retry_after, 1)

    def record_failure(self, scope: str, identifier: str) -> None:
        self._repo.record(scope, identifier, datetime.now(timezone.utc))

    def clear(self, scope: str, identifier: str) -> None:
        self._repo.clear(scope, identifier)
