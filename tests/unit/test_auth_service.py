from unittest.mock import MagicMock

import pytest

from compendium.config.settings import Settings
from compendium.domain.errors import AuthError, BusinessRuleError, ConflictError, NotFoundError
from compendium.domain.models import AppUser, Role
from compendium.services.auth import AuthService, has_permission, hash_password, verify_password


def _settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        jwt_secret_key="test-secret-key-long-enough-for-hs256",
    )


def _librarian_role() -> Role:
    role = Role(name="Librarian", permissions=["*"], is_system=True)
    role.id = 1
    return role


def _make_user(role: Role) -> AppUser:
    user = AppUser(
        username="alice",
        password_hash=hash_password("hunter2"),
        role_id=role.id,
    )
    user.id = 42
    user.role = role
    user.is_active = True
    return user


def _service(user: AppUser | None = None, role: Role | None = None) -> AuthService:
    user_repo = MagicMock()
    role_repo = MagicMock()
    if user is not None:
        user_repo.get_by_username.return_value = user
        user_repo.get.return_value = user
        user_repo.add.return_value = user
    else:
        user_repo.get_by_username.return_value = None
    if role is not None:
        role_repo.get_by_name.return_value = role
    else:
        role_repo.get_by_name.return_value = None
    return AuthService(user_repo=user_repo, role_repo=role_repo, settings=_settings())


class TestPasswordHashing:
    def test_hash_and_verify(self):
        h = hash_password("s3cret")
        assert verify_password("s3cret", h)

    def test_wrong_password_fails(self):
        h = hash_password("s3cret")
        assert not verify_password("wrong", h)

    def test_hashes_differ_for_same_password(self):
        assert hash_password("same") != hash_password("same")


class TestHasPermission:
    def test_wildcard_grants_any(self):
        assert has_permission(["*"], "item.delete")

    def test_exact_match(self):
        assert has_permission(["item.view", "loan.checkout"], "item.view")

    def test_missing_permission(self):
        assert not has_permission(["item.view"], "item.delete")

    def test_empty_permissions(self):
        assert not has_permission([], "item.view")


class TestAuthenticate:
    def test_valid_credentials(self):
        role = _librarian_role()
        user = _make_user(role)
        svc = _service(user=user)
        result = svc.authenticate("alice", "hunter2")
        assert result.username == "alice"

    def test_wrong_password_raises(self):
        role = _librarian_role()
        user = _make_user(role)
        svc = _service(user=user)
        with pytest.raises(AuthError, match="Invalid"):
            svc.authenticate("alice", "wrong")

    def test_unknown_user_raises(self):
        svc = _service()
        with pytest.raises(AuthError, match="Invalid"):
            svc.authenticate("nobody", "pw")

    def test_inactive_user_raises(self):
        role = _librarian_role()
        user = _make_user(role)
        user.is_active = False
        svc = _service(user=user)
        with pytest.raises(AuthError, match="inactive"):
            svc.authenticate("alice", "hunter2")


class TestTokenRoundtrip:
    def test_issue_and_verify(self):
        role = _librarian_role()
        user = _make_user(role)
        svc = _service(user=user)
        token = svc.issue_token(user)
        payload = svc.verify_token(token)
        assert payload["sub"] == "42"
        assert payload["username"] == "alice"
        assert payload["role"] == "Librarian"
        assert "*" in payload["permissions"]

    def test_tampered_token_raises(self):
        role = _librarian_role()
        user = _make_user(role)
        svc = _service(user=user)
        token = svc.issue_token(user) + "x"
        with pytest.raises(AuthError):
            svc.verify_token(token)


class TestCreateUser:
    def test_creates_user_with_role(self):
        role = _librarian_role()
        user_repo = MagicMock()
        user_repo.get_by_username.return_value = None
        role_repo = MagicMock()
        role_repo.get_by_name.return_value = role
        new_user = AppUser(username="bob", password_hash="x", role_id=1)
        new_user.id = 99
        user_repo.add.return_value = new_user
        svc = AuthService(user_repo=user_repo, role_repo=role_repo, settings=_settings())
        result = svc.create_user("bob", "password", "Librarian")
        assert result.username == "bob"

    def test_duplicate_username_raises(self):
        role = _librarian_role()
        user = _make_user(role)
        user_repo = MagicMock()
        user_repo.get_by_username.return_value = user
        role_repo = MagicMock()
        svc = AuthService(user_repo=user_repo, role_repo=role_repo, settings=_settings())
        with pytest.raises(ConflictError):
            svc.create_user("alice", "pw", "Librarian")

    def test_unknown_role_raises(self):
        user_repo = MagicMock()
        user_repo.get_by_username.return_value = None
        role_repo = MagicMock()
        role_repo.get_by_name.return_value = None
        svc = AuthService(user_repo=user_repo, role_repo=role_repo, settings=_settings())
        with pytest.raises(NotFoundError):
            svc.create_user("charlie", "pw", "NonExistentRole")


class TestSetPassword:
    def test_updates_hash_and_allows_login(self):
        role = _librarian_role()
        user = _make_user(role)
        user_repo = MagicMock()
        user_repo.get_by_username.return_value = user
        user_repo.update.return_value = user
        svc = AuthService(user_repo=user_repo, role_repo=MagicMock(), settings=_settings())
        svc.set_password("alice", "new-secret")
        assert verify_password("new-secret", user.password_hash)
        assert not verify_password("hunter2", user.password_hash)

    def test_unknown_user_raises(self):
        svc = _service()
        with pytest.raises(NotFoundError):
            svc.set_password("nobody", "x")

    def test_empty_password_raises(self):
        role = _librarian_role()
        user = _make_user(role)
        svc = _service(user=user)
        with pytest.raises(BusinessRuleError):
            svc.set_password("alice", "")
