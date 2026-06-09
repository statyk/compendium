"""Resolve the public base URL used to build phone-pairing QR codes.

The phone scanner pairs by scanning a QR that encodes a URL on this server.
The phone's camera (``getUserMedia``) silently fails on a non-secure origin,
so a QR pointing at a plain-``http`` non-loopback URL would be dead on arrival.
This module refuses to produce such a URL: it hard-gates on a secure context
(https, or a loopback host where browsers treat http as secure).

Precedence:

1. The ``public_base_url`` site setting, if set (non-empty). Validated for
   secure context; a non-secure override raises ``InsecureContextError``.
2. Otherwise derived from the staff request — scheme from ``X-Forwarded-Proto``
   if present (reverse-proxy TLS termination), else ``request.url.scheme``;
   host from the ``Host`` header, else ``request.url``.

The resolved base is returned normalized as ``scheme://host`` with no trailing
slash. A derived base that is not a secure context also raises
``InsecureContextError``.
"""
from __future__ import annotations

from compendium.services.site_settings import get_site_setting

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class InsecureContextError(ValueError):
    """Raised when the resolved base URL is not a secure browser context.

    The phone camera cannot start on a non-secure origin, so we refuse to
    hand out a QR that would be dead on the phone.
    """


def _hostname_only(host: str) -> str:
    """Strip an optional ``:port`` (and brackets) to get the bare hostname."""
    h = host.strip()
    if h.startswith("["):  # bracketed IPv6, e.g. [::1]:8000
        return h[1 : h.index("]")] if "]" in h else h.strip("[]")
    if ":" in h:
        return h.rsplit(":", 1)[0]
    return h


def _is_secure_context(scheme: str, host: str) -> bool:
    if scheme == "https":
        return True
    return _hostname_only(host).lower() in _LOOPBACK_HOSTS


def _normalize(scheme: str, host: str) -> str:
    return f"{scheme.lower()}://{host}".rstrip("/")


def resolve_public_base_url(request) -> str:
    """Return the normalized public base URL (``scheme://host``, no trailing slash).

    Raises ``InsecureContextError`` when the resolved base is not a secure
    browser context (not https and host not loopback).
    """
    override = get_site_setting("public_base_url")
    if override:
        scheme, sep, rest = override.strip().partition("://")
        if not sep or not rest:  # missing scheme and/or host
            raise InsecureContextError(
                "public_base_url must include a scheme and host, e.g. "
                f"https://library.example.org: {override!r}"
            )
        host = rest.rstrip("/")
        if not _is_secure_context(scheme, host):
            raise InsecureContextError(
                "public_base_url is not a secure context (must be https, or an "
                f"http loopback host): {override!r}"
            )
        return _normalize(scheme, host)

    headers = request.headers
    xfp = headers.get("x-forwarded-proto")
    scheme = xfp.split(",")[0].strip() if xfp else request.url.scheme
    host = headers.get("host") or request.url.hostname or ""
    if not _is_secure_context(scheme, host):
        raise InsecureContextError(
            "Refusing to build a phone-pairing QR over a non-secure context "
            f"({scheme}://{host}). Serve over https, or set the public_base_url "
            "site setting to an https URL."
        )
    return _normalize(scheme, host)
