"""FastAPI dependencies for the web UI layer."""

from __future__ import annotations

from datetime import timezone

import jwt
from fastapi import Cookie, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Patron
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import has_permission
from compendium.services.calendar import CalendarService

AUTH_COOKIE = "compendium_auth"


class RequiresLoginException(Exception):
    def __init__(self, next_url: str = "") -> None:
        self.next_url = next_url


class NoPatronAccountException(Exception):
    pass


def _decode_token(token: str) -> dict | None:
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            audience="compendium",
        )
    except jwt.PyJWTError:
        return None


def _check_pwd_iat(payload: dict, user: AppUser) -> bool:
    """Return False if the token's pwd_iat predates the user's password_changed_at."""
    pwd_iat = payload.get("pwd_iat")
    if pwd_iat is None or user.password_changed_at is None:
        return True
    user_ts = int(user.password_changed_at.replace(tzinfo=timezone.utc).timestamp())
    return int(pwd_iat) >= user_ts


def set_auth_cookie(response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        AUTH_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=settings.secure_cookies,
        max_age=settings.jwt_expire_minutes * 60,
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
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        return None
    user = SqlUserRepository(session).get(user_id)
    if user is None or not user.is_active:
        return None
    if not _check_pwd_iat(payload, user):
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
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _dep


def get_calendar_svc(session: Session = Depends(get_session)) -> CalendarService:
    from compendium.repositories.sql.calendar_repository import (
        SqlClosedDateRepository,
        SqlLibraryHoursRepository,
    )
    from compendium.services.site_settings import get_site_setting

    return CalendarService(
        hours_repo=SqlLibraryHoursRepository(session),
        closed_date_repo=SqlClosedDateRepository(session),
        timezone=get_site_setting("library_timezone"),
    )


def get_web_patron(
    session: Session = Depends(get_session),
    user: AppUser = Depends(require_web_user),
) -> Patron:
    patron = SqlPatronRepository(session).get_by_user_id(user.id)
    if patron is None:
        raise NoPatronAccountException()
    return patron
