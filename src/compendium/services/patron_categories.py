"""PatronCategoryService — CRUD for patron-category presets."""

from __future__ import annotations

from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import AppUser, PatronCategory
from compendium.repositories.base import PatronCategoryRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService


class PatronCategoryService:
    def __init__(
        self,
        repo: PatronCategoryRepository,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._repo = repo
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

    def list(self) -> list[PatronCategory]:
        return self._repo.list()

    def get_by_code(self, code: str) -> PatronCategory | None:
        return self._repo.get_by_code(code)

    def create(
        self, code: str, display_name: str, *, is_default: bool = False
    ) -> PatronCategory:
        code = code.strip().lower()
        display_name = display_name.strip()
        if not code:
            raise ValidationError("Category code is required.")
        if not display_name:
            raise ValidationError("Category display name is required.")
        if self._repo.get_by_code(code) is not None:
            raise BusinessRuleError(f"Category code '{code}' already exists.")
        if is_default:
            self._repo.clear_defaults()
        cat = PatronCategory(
            code=code, display_name=display_name, is_default=is_default
        )
        self._repo.add(cat)
        self._record(cat.id, AuditAction.CREATE, {"code": code, "display_name": display_name})
        return cat

    def update(
        self,
        category_id: int,
        *,
        display_name: str | None = None,
        is_default: bool | None = None,
    ) -> PatronCategory:
        cat = self._repo.get(category_id)
        if cat is None:
            raise NotFoundError(f"No patron category with id={category_id}")
        before = {"display_name": cat.display_name, "is_default": cat.is_default}
        if display_name is not None:
            new = display_name.strip()
            if not new:
                raise ValidationError("Category display name is required.")
            cat.display_name = new
        if is_default is True and not cat.is_default:
            self._repo.clear_defaults()
            cat.is_default = True
        elif is_default is False and cat.is_default:
            raise BusinessRuleError(
                "Cannot remove default from the only default category. "
                "Mark another category as default first."
            )
        self._repo.update(cat)
        self._record(
            cat.id,
            AuditAction.UPDATE,
            {"before": before, "after": {"display_name": cat.display_name, "is_default": cat.is_default}},
        )
        return cat

    def delete(self, category_id: int) -> None:
        cat = self._repo.get(category_id)
        if cat is None:
            raise NotFoundError(f"No patron category with id={category_id}")
        if cat.is_default:
            raise BusinessRuleError(
                "Cannot delete the default category. Set another as default first."
            )
        n_patrons = self._repo.count_patrons_in(cat.id)
        if n_patrons > 0:
            raise BusinessRuleError(
                f"Cannot delete category '{cat.code}': {n_patrons} patron(s) still assigned."
            )
        n_policies = self._repo.count_policies_in(cat.id)
        if n_policies > 0:
            raise BusinessRuleError(
                f"Cannot delete category '{cat.code}': {n_policies} policy/policies still reference it."
            )
        snapshot = {"code": cat.code, "display_name": cat.display_name}
        self._repo.delete(cat)
        self._record(category_id, AuditAction.DELETE, snapshot)

    def _record(self, entity_id: int | None, action: str, details: dict | None = None) -> None:
        if self._audit is not None:
            self._audit.record(
                actor=self._actor,
                actor_label=self._actor_label,
                source=self._source,
                entity_type=AuditEntityType.PATRON_CATEGORY,
                entity_id=entity_id,
                action=action,
                details=details,
            )
