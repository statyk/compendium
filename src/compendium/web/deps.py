"""FastAPI dependencies for the web UI layer."""

from __future__ import annotations

import jwt
from fastapi import Cookie, Depends, Request
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Patron
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import has_permission

AUTH_COOKIE = "compendium_auth"


class RequiresLoginException(Exception):
    def __init__(self, next_url: str = "") -> None:
        self.next_url = next_url


def _decode_token(token: str) -> dict | None:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None


def set_auth_cookie(response, token: str) -> None:
    response.set_cookie(
        AUTH_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        max_age=get_settings().jwt_expire_minutes * 60,
    )


def clear_auth_cookie(response) -> None:
    response.delete_cookie(AUTH_COOKIE)


def get_web_user(
    session: Session = Depends(get_session),
    auth: str | None = Cookie(default=None, alias=AUTH_COOKIE),
) -> AppUser | None:
    if auth is None:
        return None
    payload = _decode_token(auth)
    if payload is None:
        return None
    user = SqlUserRepository(session).get(int(payload["sub"]))
    if user is None or not user.is_active:
        return None
    return user


def require_web_user(
    request: Request,
    user: AppUser | None = Depends(get_web_user),
) -> AppUser:
    if user is None:
        raise RequiresLoginException(next_url=str(request.url.path))
    return user


def require_web_permission(permission: str):
    def _dep(
        request: Request,
        user: AppUser = Depends(require_web_user),
    ) -> AppUser:
        if not has_permission(user.role.permissions, permission):
            raise RequiresLoginException(next_url=str(request.url.path))
        return user

    return _dep


def get_web_patron(
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_web_user),
) -> Patron:
    from fastapi import HTTPException

    patron = SqlPatronRepository(session).get_by_user_id(user.id)
    if patron is None:
        raise HTTPException(status_code=403, detail="No patron account linked to your user.")
    return patron
