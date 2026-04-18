from __future__ import annotations

from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser, LoanPolicy
from compendium.repositories.base import LoanPolicyRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService


class PolicyService:
    def __init__(
        self,
        policy_repo: LoanPolicyRepository,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._policies = policy_repo
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

    def list(self) -> list[LoanPolicy]:
        return self._policies.list()

    def create(
        self,
        name: str,
        loan_period_days: int,
        max_renewals: int = 2,
        media_type_id: int | None = None,
        is_default: bool = False,
    ) -> LoanPolicy:
        if is_default:
            existing = self._policies.get_default()
            if existing is not None:
                raise BusinessRuleError(
                    "A default policy already exists. Update it instead."
                )
        policy = LoanPolicy(
            name=name,
            media_type_id=media_type_id,
            loan_period_days=loan_period_days,
            max_renewals=max_renewals,
            is_default=is_default,
        )
        self._policies.add(policy)
        self._record(
            AuditEntityType.POLICY,
            policy.id,
            AuditAction.CREATE,
            {"snapshot": {"name": name, "loan_period_days": loan_period_days, "max_renewals": max_renewals}},
        )
        return policy

    def update(
        self,
        policy_id: int,
        loan_period_days: int | None = None,
        max_renewals: int | None = None,
    ) -> LoanPolicy:
        policy = self._policies.get(policy_id)
        if policy is None:
            raise NotFoundError(f"No policy with id={policy_id}")
        before = {"loan_period_days": policy.loan_period_days, "max_renewals": policy.max_renewals}
        if loan_period_days is not None:
            policy.loan_period_days = loan_period_days
        if max_renewals is not None:
            policy.max_renewals = max_renewals
        self._policies.update(policy)
        after = {"loan_period_days": policy.loan_period_days, "max_renewals": policy.max_renewals}
        self._record(
            AuditEntityType.POLICY,
            policy.id,
            AuditAction.UPDATE,
            {"before": before, "after": after},
        )
        return policy

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
