from __future__ import annotations

from compendium.domain.errors import BusinessRuleError, ConflictError, NotFoundError
from compendium.domain.models import AppUser, Role
from compendium.repositories.base import RoleRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService


class RoleService:
    def __init__(
        self,
        role_repo: RoleRepository,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._roles = role_repo
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

    def list(self) -> list[Role]:
        return self._roles.list()

    def get(self, role_id: int) -> Role | None:
        return self._roles.get(role_id)

    def create(self, name: str, permissions: list[str]) -> Role:
        if self._roles.get_by_name(name) is not None:
            raise ConflictError(f"A role named '{name}' already exists.")
        role = Role(name=name, permissions=permissions, is_system=False)
        self._roles.add(role)
        self._record(
            AuditEntityType.ROLE,
            role.id,
            AuditAction.CREATE,
            {"snapshot": {"name": name, "permissions": permissions}},
        )
        return role

    def update(
        self,
        role_id: int,
        name: str | None = None,
        permissions: list[str] | None = None,
    ) -> Role:
        role = self._roles.get(role_id)
        if role is None:
            raise NotFoundError(f"No role with id={role_id}")
        if role.is_system:
            raise BusinessRuleError(
                f"Preset role '{role.name}' cannot be edited. Clone it to create a custom copy."
            )
        before = {"name": role.name, "permissions": list(role.permissions)}
        if name is not None and name != role.name:
            if self._roles.get_by_name(name) is not None:
                raise ConflictError(f"A role named '{name}' already exists.")
            role.name = name
        if permissions is not None:
            role.permissions = permissions
        self._roles.update(role)
        after = {"name": role.name, "permissions": list(role.permissions)}
        self._record(
            AuditEntityType.ROLE,
            role.id,
            AuditAction.UPDATE,
            {"before": before, "after": after},
        )
        return role

    def clone(self, role_id: int, new_name: str) -> Role:
        source = self._roles.get(role_id)
        if source is None:
            raise NotFoundError(f"No role with id={role_id}")
        if self._roles.get_by_name(new_name) is not None:
            raise ConflictError(f"A role named '{new_name}' already exists.")
        role = Role(name=new_name, permissions=list(source.permissions), is_system=False)
        self._roles.add(role)
        self._record(
            AuditEntityType.ROLE,
            role.id,
            AuditAction.CREATE,
            {"snapshot": {"name": new_name, "permissions": role.permissions, "cloned_from": source.name}},
        )
        return role

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
