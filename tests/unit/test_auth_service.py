from unittest.mock import MagicMock

import pytest

from compendium.config.settings import Settings
from compendium.domain.errors import AuthError, BusinessRuleError, ConflictError, NotFoundError
from compendium.domain.models import AppUser, Role
from compendium.services.auth import (
    AuthService,
    _BCRYPT_MAX_PASSWORD_BYTES,
    assignable_roles,
    has_permission,
    hash_password,
    verify_password,
)


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
            svc.set_password("nobody", "valid-test-pw!")

    def test_empty_password_raises(self):
        role = _librarian_role()
        user = _make_user(role)
        svc = _service(user=user)
        with pytest.raises(BusinessRuleError):
            svc.set_password("alice", "")


class TestChangePassword:
    def test_updates_when_current_is_correct(self):
        role = _librarian_role()
        user = _make_user(role)
        user_repo = MagicMock()
        user_repo.get_by_username.return_value = user
        user_repo.update.return_value = user
        svc = AuthService(user_repo=user_repo, role_repo=MagicMock(), settings=_settings())
        svc.change_password("alice", "hunter2", "new-secret")
        assert verify_password("new-secret", user.password_hash)

    def test_wrong_current_raises_auth_error(self):
        role = _librarian_role()
        user = _make_user(role)
        svc = _service(user=user)
        with pytest.raises(AuthError):
            svc.change_password("alice", "wrong", "new-secret")
        assert verify_password("hunter2", user.password_hash)

    def test_unknown_user_raises(self):
        svc = _service()
        with pytest.raises(NotFoundError):
            svc.change_password("nobody", "x", "y")

    def test_empty_new_password_raises(self):
        role = _librarian_role()
        user = _make_user(role)
        svc = _service(user=user)
        with pytest.raises(BusinessRuleError):
            svc.change_password("alice", "hunter2", "")


class TestAdminResetPassword:
    def _setup(self, actor_password: str = "adminpw"):
        role = _librarian_role()
        actor = AppUser(username="admin", password_hash=hash_password(actor_password), role_id=role.id)
        actor.id = 1
        actor.role = role
        actor.is_active = True
        target = AppUser(username="bob", password_hash=hash_password("oldpw"), role_id=role.id)
        target.id = 2
        target.role = role
        target.is_active = True
        user_repo = MagicMock()
        user_repo.get_by_username.side_effect = lambda u: {"admin": actor, "bob": target}.get(u)
        user_repo.update.side_effect = lambda u: u
        svc = AuthService(
            user_repo=user_repo,
            role_repo=MagicMock(),
            settings=_settings(),
            actor=actor,
        )
        return svc, actor, target

    def test_resets_target_password(self):
        svc, _, target = self._setup()
        svc.admin_reset_password(
            target_username="bob",
            actor_current_password="adminpw",
            new_password="fresh-new-pw!",
        )
        assert verify_password("fresh-new-pw!", target.password_hash)

    def test_wrong_actor_password_raises(self):
        svc, _, target = self._setup()
        with pytest.raises(AuthError):
            svc.admin_reset_password(
                target_username="bob",
                actor_current_password="wrong",
                new_password="fresh-new-pw!",
            )
        assert verify_password("oldpw", target.password_hash)

    def test_self_reset_raises(self):
        svc, actor, _ = self._setup()
        with pytest.raises(BusinessRuleError):
            svc.admin_reset_password(
                target_username="admin",
                actor_current_password="adminpw",
                new_password="fresh-new-pw!",
            )
        assert verify_password("adminpw", actor.password_hash)

    def test_missing_actor_raises(self):
        svc = _service()
        with pytest.raises(BusinessRuleError):
            svc.admin_reset_password(
                target_username="bob",
                actor_current_password="x",
                new_password="y",
            )


class TestAssignableRoles:
    def _role(self, name: str, perms: list[str], rid: int = 1) -> Role:
        r = Role(name=name, permissions=perms, is_system=True)
        r.id = rid
        return r

    def test_wildcard_actor_gets_all_roles(self):
        admin = self._role("Administrator", ["*"], 1)
        librarian = self._role("Librarian", ["patron.manage", "item.view"], 2)
        patron = self._role("Patron", ["loan.renew.self"], 3)
        all_roles = [admin, librarian, patron]
        result = assignable_roles(["*"], all_roles)
        assert {r.name for r in result} == {"Administrator", "Librarian", "Patron"}

    def test_subset_actor_excludes_superset_role(self):
        # Actor has only patron.manage — cannot assign Librarian (which adds item.view)
        librarian_role = self._role("Librarian", ["patron.manage", "item.view"], 1)
        patron_role = self._role("Patron", ["loan.renew.self"], 2)
        all_roles = [librarian_role, patron_role]
        result = assignable_roles(["patron.manage"], all_roles)
        assert not any(r.name == "Librarian" for r in result)
        assert not any(r.name == "Patron" for r in result)

    def test_actor_can_assign_subset_role(self):
        patron_role = self._role("Patron", ["loan.renew.self", "item.view"], 1)
        all_roles = [patron_role]
        result = assignable_roles(["loan.renew.self", "item.view", "patron.manage"], all_roles)
        assert any(r.name == "Patron" for r in result)

    def test_empty_role_permissions_always_assignable(self):
        readonly = self._role("ReadOnly", [], 1)
        result = assignable_roles(["item.view"], [readonly])
        assert any(r.name == "ReadOnly" for r in result)

    def test_actor_without_wildcard_cannot_assign_wildcard_role(self):
        admin = self._role("Administrator", ["*"], 1)
        result = assignable_roles(["patron.manage", "item.view"], [admin])
        assert not any(r.name == "Administrator" for r in result)


class TestBcryptCapAndTimingOracle:
    """M1 (bcrypt 72-byte cap) + M2 (timing oracle) + M7 (rounds floor)."""

    def test_hash_password_rejects_oversized(self):
        oversized = "x" * (_BCRYPT_MAX_PASSWORD_BYTES + 1)
        with pytest.raises(BusinessRuleError, match="72 bytes"):
            hash_password(oversized)

    def test_hash_password_accepts_exactly_at_cap(self):
        # Exactly 72 ASCII bytes must succeed (border case).
        boundary = "a" * _BCRYPT_MAX_PASSWORD_BYTES
        h = hash_password(boundary)
        assert verify_password(boundary, h)

    def test_verify_password_rejects_oversized(self):
        # Build a valid hash for the first 72 bytes, then verify a 73-byte input
        # that shares the same prefix — bcrypt would consider them equal without
        # the cap guard.
        prefix = "a" * _BCRYPT_MAX_PASSWORD_BYTES
        h = hash_password(prefix)
        oversized = prefix + "x"
        assert not verify_password(oversized, h)

    def test_timing_oracle_dummy_hash_runs_bcrypt(self):
        import time

        role = _librarian_role()
        user = _make_user(role)
        svc_known = _service(user=user)
        svc_unknown = _service()

        t_start = time.monotonic_ns()
        with pytest.raises(Exception):
            svc_known.authenticate("alice", "wrong_password")
        t_known = time.monotonic_ns() - t_start

        t_start = time.monotonic_ns()
        with pytest.raises(Exception):
            svc_unknown.authenticate("nobody", "wrong_password")
        t_unknown = time.monotonic_ns() - t_start

        # Both paths should exercise bcrypt; neither should be 10× faster.
        ratio = t_unknown / max(t_known, 1)
        assert 0.1 < ratio < 10, (
            f"Timing ratio unknown/known={ratio:.2f} suggests bcrypt skipped "
            f"on unknown-user path (known={t_known}ns, unknown={t_unknown}ns)"
        )

    def test_bcrypt_rounds_floor_applied(self, monkeypatch):
        import compendium.services.site_settings as ss
        monkeypatch.setenv("COMPENDIUM_BCRYPT_ROUNDS", "4")
        ss.invalidate_cache()
        try:
            h = hash_password("test-password-ok")
            # Extract the cost from the $2b$NN$ prefix — must be >= 10.
            cost = int(h.split("$")[2])
            assert cost >= 10, f"Expected cost >= 10, got {cost}"
        finally:
            monkeypatch.delenv("COMPENDIUM_BCRYPT_ROUNDS", raising=False)
            ss.invalidate_cache()


class TestPasswordStrength:
    """_validate_password_strength is called from set_password."""

    def _svc(self, user_override=None):
        role = _librarian_role()
        user = user_override or _make_user(role)
        user_repo = MagicMock()
        user_repo.get_by_username.return_value = user
        user_repo.update.return_value = user
        return AuthService(user_repo=user_repo, role_repo=MagicMock(), settings=_settings())

    def test_accepts_long_enough_password(self):
        svc = self._svc()
        svc.set_password("alice", "long-enough-pw")

    def test_rejects_too_short(self):
        svc = self._svc()
        with pytest.raises(BusinessRuleError, match="at least"):
            svc.set_password("alice", "short")

    def test_rejects_common_password(self):
        svc = self._svc()
        with pytest.raises(BusinessRuleError, match="too common"):
            svc.set_password("alice", "password")

    def test_rejects_common_password_case_insensitive(self):
        svc = self._svc()
        with pytest.raises(BusinessRuleError, match="too common"):
            svc.set_password("alice", "PASSWORD")

    def test_exactly_min_length_accepted(self):
        svc = self._svc()
        svc.set_password("alice", "Oak&Moon")  # exactly 8 chars, not in blocklist

    def test_env_override_min_length(self, monkeypatch):
        import compendium.services.site_settings as ss
        monkeypatch.setenv("COMPENDIUM_PASSWORD_MIN_LENGTH", "12")
        ss.invalidate_cache()
        svc = self._svc()
        try:
            with pytest.raises(BusinessRuleError, match="at least"):
                svc.set_password("alice", "only-9ch!")  # 9 chars < 12
        finally:
            monkeypatch.delenv("COMPENDIUM_PASSWORD_MIN_LENGTH", raising=False)
            ss.invalidate_cache()
