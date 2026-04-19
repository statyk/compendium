from unittest.mock import MagicMock

import pytest

from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import LoanPolicy
from compendium.services.policies import PolicyService


def _policy(id: int, name: str, is_default: bool = False) -> LoanPolicy:
    p = LoanPolicy(name=name, loan_period_days=14, max_renewals=2, is_default=is_default)
    p.id = id
    return p


def _svc(policy_repo=None) -> PolicyService:
    if policy_repo is None:
        policy_repo = MagicMock()
    return PolicyService(policy_repo=policy_repo)


class TestPolicyCreate:
    def test_create_non_default(self):
        repo = MagicMock()
        repo.add.side_effect = lambda p: p
        svc = _svc(repo)
        p = svc.create("DVDs", loan_period_days=7, max_renewals=1)
        assert p.name == "DVDs"
        assert p.is_default is False
        repo.clear_defaults.assert_not_called()

    def test_create_as_default_clears_existing(self):
        repo = MagicMock()
        repo.add.side_effect = lambda p: p
        svc = _svc(repo)
        svc.create("New Default", loan_period_days=21, is_default=True)
        repo.clear_defaults.assert_called_once()


class TestPolicyUpdate:
    def test_update_loan_days(self):
        existing = _policy(1, "Default", is_default=True)
        repo = MagicMock()
        repo.get.return_value = existing
        svc = _svc(repo)
        p = svc.update(1, loan_period_days=21)
        assert p.loan_period_days == 21

    def test_set_as_default_clears_others(self):
        existing = _policy(1, "Books", is_default=False)
        repo = MagicMock()
        repo.get.return_value = existing
        svc = _svc(repo)
        svc.update(1, is_default=True)
        repo.clear_defaults.assert_called_once()
        assert existing.is_default is True

    def test_unset_default_blocked_when_sole_default(self):
        existing = _policy(1, "Default", is_default=True)
        repo = MagicMock()
        repo.get.return_value = existing
        svc = _svc(repo)
        with pytest.raises(BusinessRuleError, match="Cannot remove default"):
            svc.update(1, is_default=False)

    def test_unset_non_default_is_noop(self):
        existing = _policy(1, "Books", is_default=False)
        repo = MagicMock()
        repo.get.return_value = existing
        svc = _svc(repo)
        p = svc.update(1, is_default=False)
        assert p.is_default is False
        repo.clear_defaults.assert_not_called()

    def test_not_found_raises(self):
        repo = MagicMock()
        repo.get.return_value = None
        with pytest.raises(NotFoundError):
            _svc(repo).update(99)
