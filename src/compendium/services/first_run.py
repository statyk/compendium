"""First-run 'Getting started' checklist (UX slice 5).

Computes cheap, heuristic step states from live data. Callers must gate on
permission + not-dismissed BEFORE calling (the checks are skipped entirely
once dismissed)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from sqlalchemy.orm import Session

from compendium.domain.models import Branch, ClosedDate, Item, LibraryHours, LoanPolicy
from compendium.services.site_settings import get_site_setting

_SEED_LIBRARY_NAME = "Compendium"
_SEED_BRANCH_NAME = "Main Collection"
_SEED_OPEN = time(0, 0)
_SEED_CLOSE = time(23, 59)
_SEED_POLICY = ("Default", None, 14, 2)  # name, media_type_id, period, renewals


@dataclass(frozen=True)
class FirstRunStep:
    key: str
    label: str
    url: str
    done: bool


@dataclass(frozen=True)
class FirstRunStatus:
    steps: list[FirstRunStep]

    @property
    def all_done(self) -> bool:
        return all(s.done for s in self.steps)


def first_run_status(session: Session) -> FirstRunStatus:
    branch_names = [n for (n,) in session.query(Branch.name).limit(2).all()]
    named = (
        get_site_setting("library_name") != _SEED_LIBRARY_NAME
        or len(branch_names) > 1
        or (bool(branch_names) and branch_names[0] != _SEED_BRANCH_NAME)
    )

    hours = session.query(LibraryHours).all()
    hours_touched = any(
        (not h.is_open) or h.open_time != _SEED_OPEN or h.close_time != _SEED_CLOSE
        for h in hours
    ) or session.query(ClosedDate.id).first() is not None

    policies = session.query(LoanPolicy).limit(2).all()
    policy_touched = len(policies) > 1 or any(
        (p.name, p.media_type_id, p.loan_period_days, p.max_renewals) != _SEED_POLICY
        for p in policies
    )

    has_item = session.query(Item.id).first() is not None
    email_ready = bool(get_site_setting("smtp_host"))

    return FirstRunStatus(steps=[
        FirstRunStep("name", "Name your library", "/ui/admin/settings/general", named),
        FirstRunStep("hours", "Set your open hours", "/ui/admin/library-hours", hours_touched),
        FirstRunStep("policy", "Review loan policies", "/ui/policies", policy_touched),
        FirstRunStep("item", "Add your first item", "/ui/items/new", has_item),
        FirstRunStep("email", "Set up email notices", "/ui/admin/system/smtp", email_ready),
    ])
