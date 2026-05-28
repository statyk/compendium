from __future__ import annotations

from datetime import timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from compendium.db.engine import get_settings
from compendium.db.session import get_session
from compendium.domain.models import AppUser, Patron
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import has_permission
from compendium.services.calendar import CalendarService

_bearer = HTTPBearer(auto_error=False)


def _decode_api_token(token: str) -> dict | None:
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


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: Session = Depends(get_session),
) -> AppUser:
    if creds is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = _decode_api_token(creds.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("exp") is not None:
        import time
        if payload["exp"] < time.time():
            raise HTTPException(status_code=401, detail="Token has expired")
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token") from None
    user = SqlUserRepository(session).get(user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    if not _check_pwd_iat(payload, user):
        raise HTTPException(status_code=401, detail="Session invalidated — please log in again")
    return user


def get_optional_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: Session = Depends(get_session),
) -> AppUser | None:
    if creds is None:
        return None
    payload = _decode_api_token(creds.credentials)
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


def get_current_patron(
    session: Session = Depends(get_session),
    user: AppUser = Depends(get_current_user),
) -> Patron:
    patron = SqlPatronRepository(session).get_by_user_id(user.id)
    if patron is None:
        raise HTTPException(
            status_code=403,
            detail="No patron account is linked to your user. Contact a librarian.",
        )
    return patron


def require_permission(permission: str):
    def _check(user: AppUser = Depends(get_current_user)) -> AppUser:
        if not has_permission(user.role.permissions, permission):
            raise HTTPException(status_code=403, detail="Forbidden")
        return user

    return _check


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
