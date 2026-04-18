"""Unit tests for AuditService using a mock repository."""

from unittest.mock import MagicMock

from compendium.domain.models import AppUser, AuditLog
from compendium.services.audit import AuditAction, AuditEntityType, AuditService


def _svc(repo=None):
    return AuditService(repo or MagicMock())


def _make_user(uid=1, username="librarian"):
    return AppUser(id=uid, username=username)


def test_record_with_user_sets_user_id_and_label():
    repo = MagicMock()
    svc = _svc(repo)
    user = _make_user(uid=5, username="alice")

    svc.record(
        actor=user,
        actor_label=None,
        source="api",
        entity_type=AuditEntityType.PATRON,
        entity_id=42,
        action=AuditAction.CREATE,
    )

    entry: AuditLog = repo.add.call_args[0][0]
    assert entry.user_id == 5
    assert entry.actor_label == "alice"
    assert entry.source == "api"
    assert entry.entity_type == "patron"
    assert entry.entity_id == 42
    assert entry.action == "create"


def test_record_without_user_uses_actor_label():
    repo = MagicMock()
    svc = _svc(repo)

    svc.record(
        actor=None,
        actor_label="cli:shawn",
        source="cli",
        entity_type=AuditEntityType.ITEM,
        entity_id=7,
        action=AuditAction.WITHDRAW,
        details={"snapshot": {"barcode": "000007"}},
    )

    entry: AuditLog = repo.add.call_args[0][0]
    assert entry.user_id is None
    assert entry.actor_label == "cli:shawn"
    assert entry.source == "cli"
    assert entry.details == {"snapshot": {"barcode": "000007"}}


def test_record_defaults_empty_details():
    repo = MagicMock()
    svc = _svc(repo)

    svc.record(
        actor=None,
        actor_label="system",
        source="system",
        entity_type=AuditEntityType.POLICY,
        entity_id=1,
        action=AuditAction.UPDATE,
    )

    entry: AuditLog = repo.add.call_args[0][0]
    assert entry.details == {}


def test_list_delegates_to_repo():
    repo = MagicMock()
    repo.list.return_value = []
    svc = _svc(repo)

    result = svc.list(entity_type="patron", entity_id=3, user_id=1, limit=5)

    repo.list.assert_called_once_with(entity_type="patron", entity_id=3, user_id=1, limit=5)
    assert result == []
