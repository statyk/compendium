"""Unit tests for the public base-URL resolver used to build phone-pairing QRs.

No DB, no Starlette app — the resolver only reads request headers/url and the
``public_base_url`` site setting (patched here).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from compendium.web import base_url as base_url_mod
from compendium.web.base_url import InsecureContextError, resolve_public_base_url


@dataclass
class _FakeURL:
    scheme: str
    hostname: str


@dataclass
class _FakeRequest:
    scheme: str
    host: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> _FakeURL:
        return _FakeURL(scheme=self.scheme, hostname=self.host)


def _patch_setting(monkeypatch, value):
    monkeypatch.setattr(base_url_mod, "get_site_setting", lambda key: value)


class TestOverride:
    def test_override_wins_and_is_normalized(self, monkeypatch):
        _patch_setting(monkeypatch, "https://library.example.org/")
        req = _FakeRequest(scheme="http", host="internal.lan")
        assert resolve_public_base_url(req) == "https://library.example.org"

    def test_override_http_non_loopback_raises(self, monkeypatch):
        _patch_setting(monkeypatch, "http://library.example.org")
        req = _FakeRequest(scheme="https", host="library.example.org")
        with pytest.raises(InsecureContextError):
            resolve_public_base_url(req)

    def test_override_http_localhost_allowed(self, monkeypatch):
        _patch_setting(monkeypatch, "http://localhost:8000")
        req = _FakeRequest(scheme="https", host="library.example.org")
        assert resolve_public_base_url(req) == "http://localhost:8000"


class TestDerived:
    def test_forwarded_proto_https_returns_secure(self, monkeypatch):
        _patch_setting(monkeypatch, None)
        req = _FakeRequest(
            scheme="http",
            host="library.example.org",
            headers={"x-forwarded-proto": "https", "host": "library.example.org"},
        )
        assert resolve_public_base_url(req) == "https://library.example.org"

    def test_http_non_loopback_raises(self, monkeypatch):
        _patch_setting(monkeypatch, None)
        req = _FakeRequest(scheme="http", host="library.example.org")
        with pytest.raises(InsecureContextError):
            resolve_public_base_url(req)

    def test_forwarded_proto_multi_value_takes_first(self, monkeypatch):
        _patch_setting(monkeypatch, None)
        req = _FakeRequest(
            scheme="http",
            host="library.example.org",
            headers={
                "x-forwarded-proto": "https, http",
                "host": "library.example.org",
            },
        )
        assert resolve_public_base_url(req) == "https://library.example.org"

    def test_http_localhost_allowed(self, monkeypatch):
        _patch_setting(monkeypatch, None)
        req = _FakeRequest(scheme="http", host="localhost:8000")
        assert resolve_public_base_url(req) == "http://localhost:8000"

    def test_http_uppercase_loopback_allowed(self, monkeypatch):
        _patch_setting(monkeypatch, None)
        req = _FakeRequest(scheme="http", host="LOCALHOST:8000")
        assert resolve_public_base_url(req) == "http://LOCALHOST:8000"

    def test_http_loopback_ip_allowed(self, monkeypatch):
        _patch_setting(monkeypatch, None)
        req = _FakeRequest(scheme="http", host="127.0.0.1:8000")
        assert resolve_public_base_url(req) == "http://127.0.0.1:8000"

    def test_empty_override_falls_through_to_derived(self, monkeypatch):
        _patch_setting(monkeypatch, "")
        req = _FakeRequest(scheme="https", host="library.example.org")
        assert resolve_public_base_url(req) == "https://library.example.org"
