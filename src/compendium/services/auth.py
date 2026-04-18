from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import bcrypt
import jwt

from compendium.config.settings import Settings
from compendium.domain.errors import AuthError, BusinessRuleError, ConflictError, NotFoundError
from compendium.domain.models import AppUser
from compendium.repositories.base import RoleRepository, UserRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def has_permission(permissions: list[str], required: str) -> bool:
    return "*" in permissions or required in permissions


class AuthService:
    def __init__(
        self,
        user_repo: UserRepository,
        role_repo: RoleRepository,
        settings: Settings,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._users = user_repo
        self._roles = role_repo
        self._settings = settings
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

    def create_user(
        self,
        username: str,
        password: str,
        role_name: str,
        email: str | None = None,
    ) -> AppUser:
        if self._users.get_by_username(username) is not None:
            raise ConflictError(f"Username '{username}' is already taken")
        role = self._roles.get_by_name(role_name)
        if role is None:
            raise NotFoundError(f"No role named '{role_name}'")
        user = AppUser(
            username=username,
            email=email,
            password_hash=hash_password(password),
            role_id=role.id,
        )
        result = self._users.add(user)
        self._record(
            AuditEntityType.USER,
            result.id,
            AuditAction.CREATE,
            {"snapshot": {"username": result.username, "role": role_name}},
        )
        return result

    def authenticate(self, username: str, password: str) -> AppUser:
        user = self._users.get_by_username(username)
        if user is None or not verify_password(password, user.password_hash):
            raise AuthError("Invalid username or password")
        if not user.is_active:
            raise AuthError("Account is inactive")
        return user

    def issue_token(self, user: AppUser) -> str:
        expire = datetime.utcnow() + timedelta(minutes=self._settings.jwt_expire_minutes)
        payload: dict[str, Any] = {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role.name,
            "permissions": user.role.permissions,
            "exp": expire,
        }
        return jwt.encode(
            payload,
            self._settings.jwt_secret_key,
            algorithm=self._settings.jwt_algorithm,
        )

    def deactivate_user(self, username: str) -> AppUser:
        user = self._users.get_by_username(username)
        if user is None:
            raise NotFoundError(f"No user with username '{username}'")
        if not user.is_active:
            raise BusinessRuleError(f"User '{username}' is already inactive")
        user.is_active = False
        result = self._users.update(user)
        self._record(
            AuditEntityType.USER,
            result.id,
            AuditAction.DEACTIVATE,
            {"snapshot": {"username": result.username}},
        )
        return result

    def _record(
        self,
        entity_type: str,
        entity_id: int | None,
        action: str,
        details: dict | None = None,
    ) -> None:
        if self._audit is not None:
            self._audit.record(
                actor=self._actor,
                actor_label=self._actor_label,
                source=self._source,
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                details=details,
            )

    def verify_token(self, token: str) -> dict[str, Any]:
        try:
            return jwt.decode(
                token,
                self._settings.jwt_secret_key,
                algorithms=[self._settings.jwt_algorithm],
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("Token has expired") from exc
        except jwt.PyJWTError as exc:
            raise AuthError("Invalid token") from exc
