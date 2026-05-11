"""Unit tests for rate_limit.resolve_client_ip (M9 per-IP throttle)."""
from __future__ import annotations

import pytest

from compendium.services.rate_limit import resolve_client_ip


class TestResolveClientIp:
    def test_no_trusted_proxies_returns_direct(self):
        ip = resolve_client_ip("1.2.3.4", "8.8.8.8", None)
        assert ip == "1.2.3.4"

    def test_no_trusted_proxies_ignores_xff(self):
        # Even with a plausible-looking XFF, client_host wins when no proxies configured.
        ip = resolve_client_ip("10.0.0.1", "1.2.3.4", None)
        assert ip == "10.0.0.1"

    def test_no_trusted_proxies_no_client_host(self):
        ip = resolve_client_ip(None, "1.2.3.4", None)
        assert ip is None

    def test_trusted_proxy_peels_one_hop(self):
        # Client 1.2.3.4 → trusted proxy 10.0.0.1 → app
        ip = resolve_client_ip("10.0.0.1", "1.2.3.4", "10.0.0.1")
        assert ip == "1.2.3.4"

    def test_trusted_proxy_peels_two_hops(self):
        # Client 1.2.3.4 → proxy A (10.0.0.1) → proxy B (10.0.0.2) → app
        ip = resolve_client_ip("10.0.0.2", "1.2.3.4, 10.0.0.1", "10.0.0.1,10.0.0.2")
        assert ip == "1.2.3.4"

    def test_untrusted_direct_hop_stops_walk(self):
        # Proxy not in trusted list → return client_host unchanged.
        ip = resolve_client_ip("9.9.9.9", "1.2.3.4", "10.0.0.1")
        assert ip == "9.9.9.9"

    def test_xff_spoofing_blocked_when_no_trusted_proxies(self):
        # Attacker injects X-Forwarded-For: 127.0.0.1 but no trusted proxies configured.
        ip = resolve_client_ip("evil.attacker.ip", "127.0.0.1", None)
        assert ip == "evil.attacker.ip"

    def test_xff_spoofing_blocked_when_direct_untrusted(self):
        # Attacker connects directly (not via trusted proxy) with spoofed XFF.
        ip = resolve_client_ip("attacker.ip", "127.0.0.1", "10.0.0.1")
        # "attacker.ip" is not in trusted → chain stays, return rightmost = "attacker.ip"
        assert ip == "attacker.ip"

    def test_empty_xff_with_trusted_proxy(self):
        ip = resolve_client_ip("10.0.0.1", "", "10.0.0.1")
        # chain = ["10.0.0.1"], len == 1, stop → return "10.0.0.1"
        assert ip == "10.0.0.1"

    def test_whitespace_in_xff(self):
        ip = resolve_client_ip("10.0.0.1", "  1.2.3.4  ,  10.0.0.1  ", "10.0.0.1,10.0.0.2")
        # chain after strip: ["1.2.3.4", "10.0.0.1", "10.0.0.1(direct)"]
        # pop trusted right hops until only one left
        assert ip == "1.2.3.4"
