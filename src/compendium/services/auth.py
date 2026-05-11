from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from compendium.config.settings import Settings
from compendium.domain.errors import AuthError, BusinessRuleError, ConflictError, NotFoundError
from compendium.domain.models import AppUser, Role
from compendium.repositories.base import RoleRepository, UserRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService


_WEAK_PASSWORDS = frozenset({
    "password", "password1", "password123", "passw0rd",
    "12345678", "123456789", "1234567890", "123456789a",
    "qwerty", "qwerty123", "qwertyuiop",
    "abc123", "abcdefgh",
    "letmein", "welcome", "iloveyou", "sunshine",
    "admin", "admin123", "administrator",
    "login", "login123",
    "dragon", "master", "monkey", "shadow",
    "superman", "batman",
    "compendium", "library",
})


def _validate_password_strength(password: str) -> None:
    from compendium.services.site_settings import get_site_setting
    min_len = int(get_site_setting("password_min_length"))
    if len(password) < min_len:
        raise BusinessRuleError(
            f"Password must be at least {min_len} characters long."
        )
    if password.lower() in _WEAK_PASSWORDS:
        raise BusinessRuleError(
            "Password is too common. Please choose a more unique password."
        )


def hash_password(password: str) -> str:
    from compendium.services.site_settings import get_site_setting
    rounds = int(get_site_setting("bcrypt_rounds"))
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def has_permission(permissions: list[str], required: str) -> bool:
    return "*" in permissions or required in permissions


def assignable_roles(actor_permissions: list[str], all_roles: list[Role]) -> list[Role]:
    """Return roles an actor may assign.

    Rule: actor can assign role R if every permission in R is also held by the
    actor (i.e. R.permissions ⊆ actor_permissions).  An actor with the wildcard
    '*' can assign any role, including those that also carry '*'.
    """
    if "*" in actor_permissions:
        return list(all_roles)
    actor_set = set(actor_permissions)
    return [r for r in all_roles if set(r.permissions) <= actor_set]


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
        now = datetime.now(timezone.utc)
        expire = now + timedelta(minutes=self._settings.jwt_expire_minutes)
        payload: dict[str, Any] = {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role.name,
            "permissions": user.role.permissions,
            "exp": expire,
            "iat": now,
            "aud": "compendium",
        }
        if user.password_changed_at is not None:
            payload["pwd_iat"] = int(user.password_changed_at.replace(tzinfo=timezone.utc).timestamp())
        return jwt.encode(
            payload,
            self._settings.jwt_secret_key,
            algorithm=self._settings.jwt_algorithm,
        )

    def list_users(self, limit: int = 50, offset: int = 0) -> list[AppUser]:
        return self._users.list(limit=limit, offset=offset)

    def update_role(self, username: str, role_name: str) -> AppUser:
        user = self._users.get_by_username(username)
        if user is None:
            raise NotFoundError(f"No user with username '{username}'")
        role = self._roles.get_by_name(role_name)
        if role is None:
            raise NotFoundError(f"No role named '{role_name}'")
        old_role = user.role.name
        # Update both the FK and the cached relationship — otherwise the
        # returned object still reports the old role.name on access until
        # SQLAlchemy refetches the relationship.
        user.role_id = role.id
        user.role = role
        result = self._users.update(user)
        self._record(
            AuditEntityType.USER,
            result.id,
            AuditAction.UPDATE,
            {"snapshot": {"username": result.username, "old_role": old_role, "new_role": role_name}},
        )
        return result

    def set_password(self, username: str, password: str, *, by: str = "cli") -> AppUser:
        if not password:
            raise BusinessRuleError("Password must not be empty")
        _validate_password_strength(password)
        user = self._users.get_by_username(username)
        if user is None:
            raise NotFoundError(f"No user with username '{username}'")
        user.password_hash = hash_password(password)
        user.password_changed_at = datetime.now(timezone.utc)
        result = self._users.update(user)
        self._record(
            AuditEntityType.USER,
            result.id,
            AuditAction.UPDATE,
            {"snapshot": {"username": result.username, "password_reset": True, "by": by}},
        )
        return result

    def change_password(
        self, username: str, current_password: str, new_password: str
    ) -> AppUser:
        """Self-service: the user supplies their current password to authorize."""
        user = self._users.get_by_username(username)
        if user is None:
            raise NotFoundError(f"No user with username '{username}'")
        if not verify_password(current_password, user.password_hash):
            raise AuthError("Current password is incorrect")
        return self.set_password(username, new_password, by="self")

    def admin_reset_password(
        self,
        target_username: str,
        actor_current_password: str,
        new_password: str,
    ) -> AppUser:
        """Librarian reset of another user's password. Requires the actor to
        re-authenticate with their own current password. Refuses self-reset —
        that path goes through change_password."""
        if self._actor is None:
            raise BusinessRuleError("Admin reset requires an authenticated actor")
        if target_username == self._actor.username:
            raise BusinessRuleError(
                "Use the self-service change-password page to reset your own password"
            )
        if not verify_password(actor_current_password, self._actor.password_hash):
            raise AuthError("Your current password is incorrect")
        return self.set_password(target_username, new_password, by="admin")

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

    def reactivate_user(self, username: str) -> AppUser:
        user = self._users.get_by_username(username)
        if user is None:
            raise NotFoundError(f"No user with username '{username}'")
        if user.is_active:
            raise BusinessRuleError(f"User '{username}' is already active")
        user.is_active = True
        result = self._users.update(user)
        self._record(
            AuditEntityType.USER,
            result.id,
            AuditAction.REACTIVATE,
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
                audience="compendium",
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("Token has expired") from exc
        except jwt.PyJWTError as exc:
            raise AuthError("Invalid token") from exc
