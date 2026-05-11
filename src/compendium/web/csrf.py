"""CSRF protection for state-changing web forms."""

from __future__ import annotations

import hmac
import secrets
from functools import lru_cache
from hashlib import sha256

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import HTTPException, Request

_COOKIE = "csrf_token"


@lru_cache(maxsize=4)
def _derive_csrf_secret(jwt_secret: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"compendium-csrf-v1",
    ).derive(jwt_secret.encode())


def _csrf_secret() -> bytes:
    from compendium.db.engine import get_settings

    return _derive_csrf_secret(get_settings().jwt_secret_key)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def _sign(token: str, secret: bytes) -> str:
    return hmac.new(secret, token.encode(), sha256).hexdigest()


def set_csrf_cookie(response, token: str) -> None:
    from compendium.db.engine import get_settings

    signed = f"{token}.{_sign(token, _csrf_secret())}"
    response.set_cookie(
        _COOKIE,
        signed,
        httponly=True,
        samesite="strict",
        secure=get_settings().secure_cookies,
    )


def get_csrf_token(request: Request) -> str:
    """Returns the raw token for embedding in templates.  Raises if cookie absent/invalid."""
    cookie_val = request.cookies.get(_COOKIE)
    if not cookie_val or "." not in cookie_val:
        raise HTTPException(status_code=403, detail="CSRF cookie missing or malformed")
    raw, sig = cookie_val.rsplit(".", 1)
    if not hmac.compare_digest(_sign(raw, _csrf_secret()), sig):
        raise HTTPException(status_code=403, detail="CSRF cookie invalid")
    return raw


def ensure_csrf(request: Request) -> tuple[str, str | None]:
    """Returns (token, new_token_or_None).  Call set_csrf_cookie if new_token is not None."""
    try:
        return get_csrf_token(request), None
    except HTTPException:
        fresh = generate_token()
        return fresh, fresh


def check_csrf_form(request: Request, token: str) -> None:
    """Call in POST handlers to validate the _csrf form field against the signed cookie."""
    cookie_val = request.cookies.get(_COOKIE)
    if not cookie_val or "." not in cookie_val:
        raise HTTPException(status_code=403, detail="CSRF token missing")
    raw, sig = cookie_val.rsplit(".", 1)
    if not hmac.compare_digest(_sign(raw, _csrf_secret()), sig):
        raise HTTPException(status_code=403, detail="CSRF signature invalid")
    if not hmac.compare_digest(token, raw):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")
