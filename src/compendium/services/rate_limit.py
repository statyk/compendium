"""Per-identity and per-IP login rate limiting.

Sliding-window throttle backed by the failed_login table so it coordinates
across multiple worker processes without a Redis dependency.

Scopes
------
- ``login_user``  — username on /auth/login and /ui/login
- ``login_ip``    — client IP on /auth/login and /ui/login
- ``kiosk_card``  — library card number on /ui/kiosk/start

Design notes
------------
- Identity-keyed (login_user / kiosk_card): per-username sliding window.
  Behind a reverse proxy request.client.host is the proxy's IP; use
  COMPENDIUM_TRUSTED_PROXIES to honor X-Forwarded-For so the IP limit
  keys on the real client rather than the proxy address.
- IP-keyed (login_ip): looser threshold (default 30 / 5 min) to catch
  single-source credential-stuffing across many usernames without
  false-positiving shared NAT'd networks. Set login_max_failures_per_ip=0
  to disable if relying solely on the edge tier (nginx limit_req).
- Sliding window: once count >= max_failures in the last window_seconds,
  the next attempt is blocked.  New failures inside the window do NOT extend
  the lockout — the window advances naturally so the oldest failure ages out.
- clear() / clear_ip() are called on successful login so stale failures
  don't persist after a legitimate login.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from compendium.repositories.sql.failed_login_repository import SqlFailedLoginRepository
from compendium.services.site_settings import get_site_setting


def resolve_client_ip(
    client_host: str | None,
    forwarded_for: str,
    trusted_proxies_raw: str | None,
) -> str | None:
    """Return the real client IP, honoring X-Forwarded-For only when trusted_proxies is set.

    When trusted_proxies_raw is None the direct connection address is used
    so a forged X-Forwarded-For header cannot spoof the rate-limit key.
    When set, the function walks the XFF chain right-to-left, popping hops
    that belong to trusted proxies, and returns the first untrusted hop.
    """
    if not trusted_proxies_raw:
        return client_host
    trusted = {p.strip() for p in trusted_proxies_raw.split(",") if p.strip()}
    chain = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    if client_host:
        chain.append(client_host)
    # Pop rightmost hops while they are trusted proxies; keep at least one entry.
    while len(chain) > 1 and chain[-1] in trusted:
        chain.pop()
    return chain[-1] if chain else client_host


class RateLimitService:
    def __init__(self, repo: SqlFailedLoginRepository) -> None:
        self._repo = repo

    def _policy(self) -> tuple[int, int]:
        """Return (max_failures, window_seconds) from site settings."""
        return (
            int(get_site_setting("login_max_failures")),
            int(get_site_setting("login_failure_window_seconds")),
        )

    def _ip_policy(self) -> tuple[int, int]:
        """Return (max_failures, window_seconds) for the per-IP limit."""
        return (
            int(get_site_setting("login_max_failures_per_ip")),
            int(get_site_setting("login_failure_window_seconds_per_ip")),
        )

    def _check(self, scope: str, identifier: str, max_failures: int, window_seconds: int) -> int | None:
        if max_failures == 0:
            return None
        now = datetime.now(timezone.utc)
        since = now - timedelta(seconds=window_seconds)
        count, oldest = self._repo.count_and_oldest(scope, identifier, since)
        if count < max_failures or oldest is None:
            return None
        oldest_aware = oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)
        retry_after = math.ceil(
            (oldest_aware + timedelta(seconds=window_seconds) - now).total_seconds()
        )
        return max(retry_after, 1)

    def check(self, scope: str, identifier: str) -> int | None:
        """Return seconds until next attempt is allowed, or None if not blocked.

        A return value > 0 means the caller should reply with 429 and set a
        Retry-After header to this value.  Returns None when max_failures == 0
        (throttling disabled).
        """
        max_failures, window_seconds = self._policy()
        return self._check(scope, identifier, max_failures, window_seconds)

    def check_ip(self, ip: str) -> int | None:
        """Per-IP variant of check(); uses the login_max_failures_per_ip policy."""
        max_failures, window_seconds = self._ip_policy()
        return self._check("login_ip", ip, max_failures, window_seconds)

    def record_failure(self, scope: str, identifier: str) -> None:
        self._repo.record(scope, identifier, datetime.now(timezone.utc))

    def record_ip_failure(self, ip: str) -> None:
        self._repo.record("login_ip", ip, datetime.now(timezone.utc))

    def clear(self, scope: str, identifier: str) -> None:
        self._repo.clear(scope, identifier)

    def clear_ip(self, ip: str) -> None:
        self._repo.clear("login_ip", ip)
