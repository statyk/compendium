from unittest.mock import MagicMock

import pytest

from compendium.domain.errors import BusinessRuleError, ConflictError, NotFoundError
from compendium.domain.models import Role
from compendium.services.roles import RoleService


def _role(id: int, name: str, permissions: list[str], is_system: bool = False) -> Role:
    r = Role(name=name, permissions=permissions, is_system=is_system)
    r.id = id
    return r


def _svc(role_repo=None) -> RoleService:
    if role_repo is None:
        role_repo = MagicMock()
        role_repo.get_by_name.return_value = None
    return RoleService(role_repo=role_repo)


class TestRoleCreate:
    def test_create_custom_role(self):
        repo = MagicMock()
        repo.get_by_name.return_value = None
        repo.add.side_effect = lambda r: r
        svc = RoleService(role_repo=repo)
        role = svc.create("Custom", ["item.view", "loan.checkout"])
        assert role.name == "Custom"
        assert role.permissions == ["item.view", "loan.checkout"]
        assert role.is_system is False

    def test_create_full_access(self):
        repo = MagicMock()
        repo.get_by_name.return_value = None
        repo.add.side_effect = lambda r: r
        role = RoleService(role_repo=repo).create("SuperAdmin", ["*"])
        assert role.permissions == ["*"]

    def test_duplicate_name_raises(self):
        repo = MagicMock()
        repo.get_by_name.return_value = _role(1, "Existing", [])
        with pytest.raises(ConflictError):
            RoleService(role_repo=repo).create("Existing", [])


class TestRoleUpdate:
    def test_update_custom_role_name(self):
        existing = _role(1, "OldName", ["item.view"])
        repo = MagicMock()
        repo.get.return_value = existing
        repo.get_by_name.return_value = None
        svc = RoleService(role_repo=repo)
        r = svc.update(1, name="NewName")
        assert r.name == "NewName"

    def test_update_permissions(self):
        existing = _role(1, "Custom", ["item.view"])
        repo = MagicMock()
        repo.get.return_value = existing
        repo.get_by_name.return_value = None
        r = RoleService(role_repo=repo).update(1, permissions=["item.view", "loan.checkout"])
        assert "loan.checkout" in r.permissions

    def test_system_role_update_blocked(self):
        existing = _role(1, "Librarian", ["*"], is_system=True)
        repo = MagicMock()
        repo.get.return_value = existing
        with pytest.raises(BusinessRuleError, match="cannot be edited"):
            RoleService(role_repo=repo).update(1, name="NewLib")

    def test_duplicate_name_on_update_raises(self):
        existing = _role(1, "Custom", ["item.view"])
        other = _role(2, "Taken", [])
        repo = MagicMock()
        repo.get.return_value = existing
        repo.get_by_name.return_value = other
        with pytest.raises(ConflictError):
            RoleService(role_repo=repo).update(1, name="Taken")

    def test_not_found_raises(self):
        repo = MagicMock()
        repo.get.return_value = None
        with pytest.raises(NotFoundError):
            RoleService(role_repo=repo).update(99, name="X")


class TestRoleClone:
    def test_clone_preset_strips_is_system(self):
        source = _role(1, "Librarian", ["*"], is_system=True)
        repo = MagicMock()
        repo.get.return_value = source
        repo.get_by_name.return_value = None
        repo.add.side_effect = lambda r: r
        cloned = RoleService(role_repo=repo).clone(1, "Librarian (copy)")
        assert cloned.name == "Librarian (copy)"
        assert cloned.permissions == ["*"]
        assert cloned.is_system is False

    def test_clone_duplicate_name_raises(self):
        source = _role(1, "Patron", ["item.view"], is_system=True)
        existing = _role(2, "Patron (copy)", [])
        repo = MagicMock()
        repo.get.return_value = source
        repo.get_by_name.return_value = existing
        with pytest.raises(ConflictError):
            RoleService(role_repo=repo).clone(1, "Patron (copy)")

    def test_clone_not_found_raises(self):
        repo = MagicMock()
        repo.get.return_value = None
        with pytest.raises(NotFoundError):
            RoleService(role_repo=repo).clone(99, "X")
