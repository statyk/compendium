"""Tests for the cover proxy: service module, web route, CLI prune."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from compendium.cli.commands.maintenance import app as maintenance_app
from compendium.services import covers
from compendium.services.covers import (
    CoverNotFound,
    DisallowedHost,
    cache_dir,
    cache_key,
    fetch_or_404,
    host_allowed,
    prune,
)
from compendium.web.routes.covers import router as covers_router


class _FakeResp:
    """Duck-typed stand-in for ``httpx.Response`` with just what fetch_or_404 reads."""

    def __init__(
        self,
        url: str = "https://covers.openlibrary.org/b/id/1-L.jpg",
        status: int = 200,
        content: bytes = b"\xff\xd8\xff\xe0",
        content_type: str = "image/jpeg",
        history=(),
    ):
        self.url = url
        self.status_code = status
        self.content = content
        self.headers = {"content-type": content_type}
        self.history = list(history)


@pytest.fixture
def cache_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_COVER_CACHE_DIR", str(tmp_path))
    return tmp_path


def _patch_httpx_get(monkeypatch, fn):
    monkeypatch.setattr("compendium.services.covers.httpx.get", fn)


# ── host_allowed ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://covers.openlibrary.org/b/id/1-L.jpg", True),
        ("https://image.tmdb.org/t/p/w500/x.jpg", True),
        ("https://coverartarchive.org/release/xyz/front", True),
        ("https://archive.org/download/foo/x.jpg", True),
        ("https://ia600507.us.archive.org/view_archive.php", True),
        ("https://evil.example.com/x.jpg", False),
        ("http://covers.openlibrary.org.evil.com/", False),
        ("https://", False),
    ],
)
def test_host_allowed(url, expected):
    assert host_allowed(url) is expected


def test_cache_key_is_stable_and_bounded():
    k1 = cache_key("https://covers.openlibrary.org/b/id/1-L.jpg")
    k2 = cache_key("https://covers.openlibrary.org/b/id/1-L.jpg")
    k3 = cache_key("https://covers.openlibrary.org/b/id/2-L.jpg")
    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 32


def test_cache_dir_honors_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_COVER_CACHE_DIR", str(tmp_path / "elsewhere"))
    d = cache_dir()
    assert d == tmp_path / "elsewhere"
    assert d.is_dir()


# ── fetch_or_404 ──────────────────────────────────────────────────────────────

def test_disallowed_initial_url_raises(cache_tmp, monkeypatch):
    called = {"n": 0}

    def _fail(*a, **kw):
        called["n"] += 1
        raise AssertionError("should not fetch")

    _patch_httpx_get(monkeypatch, _fail)

    with pytest.raises(DisallowedHost):
        fetch_or_404("https://evil.example.com/x.jpg")
    assert called["n"] == 0


def test_non_http_url_raises(cache_tmp):
    with pytest.raises(DisallowedHost):
        fetch_or_404("file:///etc/passwd")


def test_fetch_miss_stores_and_returns_path(cache_tmp, monkeypatch):
    url = "https://covers.openlibrary.org/b/id/42-L.jpg"
    _patch_httpx_get(monkeypatch, lambda *a, **kw: _FakeResp(url=url, content=b"IMGBYTES"))

    path = fetch_or_404(url)

    assert path.exists()
    assert path.read_bytes() == b"IMGBYTES"
    assert path.parent == cache_tmp


def test_fetch_hit_short_circuits(cache_tmp, monkeypatch):
    url = "https://covers.openlibrary.org/b/id/42-L.jpg"
    existing = cache_tmp / f"{cache_key(url)}.jpg"
    existing.write_bytes(b"CACHED")

    def _should_not_fetch(*a, **kw):
        raise AssertionError("cache hit should not fetch")

    _patch_httpx_get(monkeypatch, _should_not_fetch)

    assert fetch_or_404(url).read_bytes() == b"CACHED"


def test_negative_cache_blocks_refetch_within_ttl(cache_tmp, monkeypatch):
    url = "https://covers.openlibrary.org/b/id/42-L.jpg"
    (cache_tmp / f"{cache_key(url)}.404").touch()

    def _should_not_fetch(*a, **kw):
        raise AssertionError("negative cache should prevent fetch")

    _patch_httpx_get(monkeypatch, _should_not_fetch)

    with pytest.raises(CoverNotFound):
        fetch_or_404(url)


def test_negative_cache_expires_and_refetches(cache_tmp, monkeypatch):
    url = "https://covers.openlibrary.org/b/id/42-L.jpg"
    stale = cache_tmp / f"{cache_key(url)}.404"
    stale.touch()
    stale_age = time.time() - (covers.NEGATIVE_TTL_SECONDS + 60)
    import os
    os.utime(stale, (stale_age, stale_age))

    _patch_httpx_get(monkeypatch, lambda *a, **kw: _FakeResp(url=url, content=b"FRESH"))

    assert fetch_or_404(url).read_bytes() == b"FRESH"


def test_non_image_content_type_marks_negative(cache_tmp, monkeypatch):
    url = "https://covers.openlibrary.org/b/id/42-L.jpg"
    _patch_httpx_get(
        monkeypatch,
        lambda *a, **kw: _FakeResp(url=url, content=b"<html>", content_type="text/html"),
    )

    with pytest.raises(CoverNotFound):
        fetch_or_404(url)
    assert (cache_tmp / f"{cache_key(url)}.404").exists()


def test_4xx_response_marks_negative(cache_tmp, monkeypatch):
    url = "https://covers.openlibrary.org/b/id/42-L.jpg"
    _patch_httpx_get(
        monkeypatch,
        lambda *a, **kw: _FakeResp(url=url, status=404, content=b"", content_type="image/jpeg"),
    )

    with pytest.raises(CoverNotFound):
        fetch_or_404(url)
    assert (cache_tmp / f"{cache_key(url)}.404").exists()


def test_httpx_error_marks_negative(cache_tmp, monkeypatch):
    url = "https://covers.openlibrary.org/b/id/42-L.jpg"

    def _boom(*a, **kw):
        raise httpx.ConnectError("no network")

    _patch_httpx_get(monkeypatch, _boom)

    with pytest.raises(CoverNotFound):
        fetch_or_404(url)
    assert (cache_tmp / f"{cache_key(url)}.404").exists()


def test_redirect_to_disallowed_host_rejects(cache_tmp, monkeypatch):
    url = "https://covers.openlibrary.org/b/id/42-L.jpg"
    # Final resp URL is on a disallowed host — simulates a redirect chain that
    # ends off-allowlist (e.g., openlibrary → evil.com).
    bad_final = _FakeResp(
        url="https://evil.example.com/x.jpg",
        history=[_FakeResp(url=url)],  # first hop was allowed
    )
    _patch_httpx_get(monkeypatch, lambda *a, **kw: bad_final)

    with pytest.raises(DisallowedHost):
        fetch_or_404(url)


def test_redirect_chain_within_allowlist_succeeds(cache_tmp, monkeypatch):
    url = "https://covers.openlibrary.org/b/id/42-L.jpg"
    final = _FakeResp(
        url="https://ia600507.us.archive.org/view_archive.php?file=x.jpg",
        content=b"JPEG",
        history=[_FakeResp(url=url), _FakeResp(url="https://archive.org/download/foo/x.jpg")],
    )
    _patch_httpx_get(monkeypatch, lambda *a, **kw: final)

    path = fetch_or_404(url)

    assert path.read_bytes() == b"JPEG"


# ── prune ─────────────────────────────────────────────────────────────────────

def test_prune_noop_when_under_cap(cache_tmp):
    (cache_tmp / "a.jpg").write_bytes(b"x" * 100)
    (cache_tmp / "b.jpg").write_bytes(b"x" * 100)

    removed, freed = prune(max_bytes=10_000)

    assert (removed, freed) == (0, 0)
    assert (cache_tmp / "a.jpg").exists()
    assert (cache_tmp / "b.jpg").exists()


def test_prune_evicts_oldest_first(cache_tmp):
    import os

    older = cache_tmp / "old.jpg"
    newer = cache_tmp / "new.jpg"
    older.write_bytes(b"x" * 600)
    newer.write_bytes(b"x" * 600)
    past = time.time() - 3600
    os.utime(older, (past, past))

    removed, freed = prune(max_bytes=700)

    assert removed == 1
    assert freed == 600
    assert not older.exists()
    assert newer.exists()


# ── Web route ────────────────────────────────────────────────────────────────

@pytest.fixture
def proxy_client():
    app = FastAPI()
    app.include_router(covers_router, prefix="/ui")
    with TestClient(app) as c:
        yield c


def test_route_returns_image_on_success(cache_tmp, proxy_client, monkeypatch):
    url = "https://covers.openlibrary.org/b/id/42-L.jpg"
    _patch_httpx_get(monkeypatch, lambda *a, **kw: _FakeResp(url=url, content=b"JPEGBYTES"))

    resp = proxy_client.get("/ui/covers", params={"url": url})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")
    assert "max-age" in resp.headers.get("cache-control", "")
    assert resp.content == b"JPEGBYTES"


def test_route_rejects_disallowed_host(cache_tmp, proxy_client):
    resp = proxy_client.get("/ui/covers", params={"url": "https://evil.example.com/x.jpg"})

    assert resp.status_code == 400


def test_route_returns_404_on_upstream_miss(cache_tmp, proxy_client, monkeypatch):
    url = "https://covers.openlibrary.org/b/id/42-L.jpg"
    _patch_httpx_get(
        monkeypatch,
        lambda *a, **kw: _FakeResp(url=url, status=404, content=b"", content_type="image/jpeg"),
    )

    resp = proxy_client.get("/ui/covers", params={"url": url})

    assert resp.status_code == 404


def test_route_requires_url_param(proxy_client):
    resp = proxy_client.get("/ui/covers")
    assert resp.status_code == 422


# ── CLI prune-cover-cache ─────────────────────────────────────────────────────

def test_cli_prune_errors_on_zero_max(cache_tmp):
    result = CliRunner().invoke(maintenance_app, ["prune-cover-cache", "--max-mb", "0"])
    assert result.exit_code == 1


def test_cli_prune_noop_on_empty_cache(cache_tmp):
    result = CliRunner().invoke(maintenance_app, ["prune-cover-cache", "--max-mb", "10"])
    assert result.exit_code == 0
    assert "nothing to prune" in result.output


def test_cli_prune_removes_and_reports(cache_tmp):
    import os

    a = cache_tmp / "a.jpg"
    b = cache_tmp / "b.jpg"
    a.write_bytes(b"x" * 600_000)
    b.write_bytes(b"x" * 600_000)
    past = time.time() - 3600
    os.utime(a, (past, past))

    result = CliRunner().invoke(maintenance_app, ["prune-cover-cache", "--max-mb", "1"])

    assert result.exit_code == 0
    assert "Pruned 1" in result.output
    assert not a.exists()
    assert b.exists()
