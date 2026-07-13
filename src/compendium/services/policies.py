from __future__ import annotations

from compendium.domain.errors import BusinessRuleError, NotFoundError
from compendium.domain.models import AppUser, LoanPolicy
from compendium.repositories.base import LoanPolicyRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService

_MISSING = object()


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
        patron_category_id: int | None = None,
        is_default: bool = False,
    ) -> LoanPolicy:
        if is_default:
            self._policies.clear_defaults()
        policy = LoanPolicy(
            name=name,
            media_type_id=media_type_id,
            patron_category_id=patron_category_id,
            loan_period_days=loan_period_days,
            max_renewals=max_renewals,
            is_default=is_default,
        )
        self._policies.add(policy)
        self._record(
            AuditEntityType.POLICY,
            policy.id,
            AuditAction.CREATE,
            {"snapshot": {
                "name": name,
                "loan_period_days": loan_period_days,
                "max_renewals": max_renewals,
                "media_type_id": media_type_id,
                "patron_category_id": patron_category_id,
            }},
        )
        return policy

    def update(
        self,
        policy_id: int,
        loan_period_days: int | None = None,
        max_renewals: int | None = None,
        is_default: bool | None = None,
        *,
        patron_category_id: int | None | object = _MISSING,
        overdue_fine_per_day_cents: int | None | object = _MISSING,
        overdue_fine_cap_cents: int | None | object = _MISSING,
        grace_period_days: int | None = None,
        lost_item_default_cents: int | None | object = _MISSING,
        lost_item_processing_fee_cents: int | None | object = _MISSING,
    ) -> LoanPolicy:
        policy = self._policies.get(policy_id)
        if policy is None:
            raise NotFoundError(f"No policy with id={policy_id}")
        before = {
            "loan_period_days": policy.loan_period_days,
            "max_renewals": policy.max_renewals,
            "is_default": policy.is_default,
            "patron_category_id": policy.patron_category_id,
            "overdue_fine_per_day_cents": policy.overdue_fine_per_day_cents,
            "overdue_fine_cap_cents": policy.overdue_fine_cap_cents,
            "grace_period_days": policy.grace_period_days,
            "lost_item_default_cents": policy.lost_item_default_cents,
            "lost_item_processing_fee_cents": policy.lost_item_processing_fee_cents,
        }
        if loan_period_days is not None:
            policy.loan_period_days = loan_period_days
        if max_renewals is not None:
            policy.max_renewals = max_renewals
        if is_default is True:
            self._policies.clear_defaults()
            policy.is_default = True
        elif is_default is False:
            if policy.is_default:
                raise BusinessRuleError(
                    "Cannot remove default from the only default policy. "
                    "Set another policy as default first."
                )
            policy.is_default = False
        if patron_category_id is not _MISSING:
            policy.patron_category_id = patron_category_id  # type: ignore[assignment]
        if overdue_fine_per_day_cents is not _MISSING:
            policy.overdue_fine_per_day_cents = overdue_fine_per_day_cents
        if overdue_fine_cap_cents is not _MISSING:
            policy.overdue_fine_cap_cents = overdue_fine_cap_cents
        if grace_period_days is not None:
            if grace_period_days < 0:
                raise BusinessRuleError("grace_period_days must be >= 0")
            policy.grace_period_days = grace_period_days
        if lost_item_default_cents is not _MISSING:
            policy.lost_item_default_cents = lost_item_default_cents
        if lost_item_processing_fee_cents is not _MISSING:
            policy.lost_item_processing_fee_cents = lost_item_processing_fee_cents
        self._policies.update(policy)
        after = {
            "loan_period_days": policy.loan_period_days,
            "max_renewals": policy.max_renewals,
            "is_default": policy.is_default,
            "patron_category_id": policy.patron_category_id,
            "overdue_fine_per_day_cents": policy.overdue_fine_per_day_cents,
            "overdue_fine_cap_cents": policy.overdue_fine_cap_cents,
            "grace_period_days": policy.grace_period_days,
            "lost_item_default_cents": policy.lost_item_default_cents,
            "lost_item_processing_fee_cents": policy.lost_item_processing_fee_cents,
        }
        self._record(
            AuditEntityType.POLICY,
            policy.id,
            AuditAction.UPDATE,
            {"before": before, "after": after},
        )
        return policy

    def delete(self, policy_id: int) -> None:
        policy = self._policies.get(policy_id)
        if policy is None:
            raise NotFoundError(f"No policy with id={policy_id}")
        if policy.is_default:
            raise BusinessRuleError(
                "Cannot delete the default policy. "
                "Set another policy as default first."
            )
        snapshot = {
            "name": policy.name,
            "media_type_id": policy.media_type_id,
            "patron_category_id": policy.patron_category_id,
            "loan_period_days": policy.loan_period_days,
            "max_renewals": policy.max_renewals,
        }
        self._policies.delete(policy)
        self._record(
            AuditEntityType.POLICY, policy_id, AuditAction.DELETE, snapshot
        )

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
