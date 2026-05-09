"""Notification queuing, rendering, and delivery.

Outbox pattern: every notification is recorded as a row at queue time (with
subject/body pre-rendered), then a drainer (typically a cron-invoked CLI
command) sends pending rows via SMTP.

Templates live in ``services/notifications/templates/<template_key>/``
as ``subject.txt`` + ``body.txt`` (Jinja). Context fields are snapshotted
at queue time so later edits to the source data don't retroactively rewrite
already-queued messages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from compendium.config.settings import Settings
from compendium.domain.enums import (
    HoldStatus,
    NotificationStatus,
    NotificationTemplate,
)
from compendium.domain.errors import NotFoundError, ValidationError
from compendium.domain.models import AppUser, Hold, Loan, Notification, Patron
from compendium.repositories.base import (
    HoldRepository,
    LoanRepository,
    NotificationRepository,
    PatronRepository,
)
from compendium.services.audit import AuditAction, AuditEntityType, AuditService
from compendium.services.notifications.smtp import SMTPSender
from compendium.services.site_settings import get_site_setting

_log = logging.getLogger("compendium.notifications")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    undefined=StrictUndefined,
    autoescape=False,
    keep_trailing_newline=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _render(template_key: str, fragment: str, context: dict) -> str:
    t = _jinja_env.get_template(f"{template_key}/{fragment}")
    return t.render(**context).strip()


@dataclass
class NotificationCounts:
    sent: int = 0
    failed: int = 0
    cancelled: int = 0
    skipped: int = 0
    queued: int = 0
    deleted: int = 0


class NotificationService:
    def __init__(
        self,
        *,
        notification_repo: NotificationRepository,
        loan_repo: LoanRepository,
        hold_repo: HoldRepository,
        patron_repo: PatronRepository,
        settings: Settings,
        sender: SMTPSender | None = None,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._notifications = notification_repo
        self._loans = loan_repo
        self._holds = hold_repo
        self._patrons = patron_repo
        self._settings = settings
        self._sender = sender
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

    # ------------------------------------------------------------------
    # Queuing — per-row helpers
    # ------------------------------------------------------------------

    def queue_hold_ready(self, hold: Hold) -> Notification | None:
        patron = hold.patron
        if not self._patron_can_receive(patron):
            return None
        existing = self._notifications.get_existing(
            loan_id=None,
            hold_id=hold.id,
            template_key=NotificationTemplate.HOLD_READY.value,
            discriminator=0,
        )
        if existing is not None:
            return None
        work = hold.work
        branch = hold.branch
        ctx = {
            "patron_name": patron.full_name,
            "work_title": work.title if work else "(unknown work)",
            "branch_name": branch.name if branch else "the library",
            "expires_at": hold.expires_at,
            "library_name": get_site_setting("library_name"),
        }
        return self._insert(
            template=NotificationTemplate.HOLD_READY,
            context=ctx,
            patron=patron,
            loan_id=None,
            hold_id=hold.id,
            discriminator=0,
        )

    def queue_due_soon(self, loan: Loan) -> Notification | None:
        patron = loan.patron
        if not self._patron_can_receive(patron):
            return None
        discriminator = loan.renewal_count  # separate notice per renewal cycle
        existing = self._notifications.get_existing(
            loan_id=loan.id,
            hold_id=None,
            template_key=NotificationTemplate.DUE_SOON.value,
            discriminator=discriminator,
        )
        if existing is not None:
            return None
        delta = loan.due_at - datetime.now(tz=timezone.utc)
        days_before = max(0, delta.days)
        item = loan.item
        work = item.work if item else None
        ctx = {
            "patron_name": patron.full_name,
            "work_title": work.title if work else "(unknown work)",
            "item_barcode": item.barcode if item else "",
            "due_at": loan.due_at,
            "days_before": days_before,
            "library_name": get_site_setting("library_name"),
        }
        return self._insert(
            template=NotificationTemplate.DUE_SOON,
            context=ctx,
            patron=patron,
            loan_id=loan.id,
            hold_id=None,
            discriminator=discriminator,
        )

    def queue_overdue(self, loan: Loan, tier: int) -> Notification | None:
        patron = loan.patron
        if not self._patron_can_receive(patron):
            return None
        existing = self._notifications.get_existing(
            loan_id=loan.id,
            hold_id=None,
            template_key=NotificationTemplate.OVERDUE.value,
            discriminator=tier,
        )
        if existing is not None:
            return None
        now = datetime.now(tz=timezone.utc)
        days_late = max(0, (now - loan.due_at).days)
        item = loan.item
        work = item.work if item else None
        ctx = {
            "patron_name": patron.full_name,
            "work_title": work.title if work else "(unknown work)",
            "item_barcode": item.barcode if item else "",
            "due_at": loan.due_at,
            "days_late": days_late,
            "tier": tier,
            "library_name": get_site_setting("library_name"),
        }
        return self._insert(
            template=NotificationTemplate.OVERDUE,
            context=ctx,
            patron=patron,
            loan_id=loan.id,
            hold_id=None,
            discriminator=tier,
        )

    # ------------------------------------------------------------------
    # Batch queuing — cron helpers
    # ------------------------------------------------------------------

    def queue_due_soon_batch(self, days_before: int) -> NotificationCounts:
        counts = NotificationCounts()
        for loan in self._loans.list_due_within(days=days_before):
            if self.queue_due_soon(loan) is not None:
                counts.queued += 1
        return counts

    def queue_overdue_batch(self, tiers: list[int]) -> NotificationCounts:
        counts = NotificationCounts()
        now = datetime.now(tz=timezone.utc)
        # Send the highest tier the patron qualifies for; we don't want to
        # fire both "tier 1" and "tier 3" on a loan that's 30+ days late.
        tiers_desc = sorted(tiers, reverse=True)
        for loan in self._loans.list_active_overdue():
            days_late = (now - loan.due_at).days
            for tier in tiers_desc:
                if days_late >= tier:
                    if self.queue_overdue(loan, tier) is not None:
                        counts.queued += 1
                    break
        return counts

    # ------------------------------------------------------------------
    # Drainer
    # ------------------------------------------------------------------

    def send_pending(
        self, batch_size: int | None = None, dry_run: bool = False
    ) -> NotificationCounts:
        counts = NotificationCounts()
        batch = self._notifications.list_pending(
            limit=batch_size or get_site_setting("notifications_batch_size")
        )
        if not batch:
            self._record_send_summary(counts)
            return counts

        if dry_run:
            counts.skipped = len(batch)
            return counts

        if self._sender is None or not self._sender.is_configured():
            counts.skipped = len(batch)
            _log.warning(
                "SMTP not configured — %d notification(s) remain pending",
                counts.skipped,
            )
            self._record_send_summary(counts)
            return counts

        max_attempts = get_site_setting("notifications_max_attempts")
        for row in batch:
            if not row.recipient_email:
                row.status = NotificationStatus.CANCELLED.value
                row.last_error = "no_email"
                self._notifications.update(row)
                counts.cancelled += 1
                continue
            try:
                self._sender.send(
                    to=row.recipient_email, subject=row.subject, body=row.body
                )
                row.status = NotificationStatus.SENT.value
                row.sent_at = datetime.now(tz=timezone.utc)
                self._notifications.update(row)
                counts.sent += 1
            except Exception as exc:
                row.attempts += 1
                row.last_error = str(exc)
                if row.attempts >= max_attempts:
                    row.status = NotificationStatus.FAILED.value
                    counts.failed += 1
                self._notifications.update(row)
        self._record_send_summary(counts)
        return counts

    # ------------------------------------------------------------------
    # Admin actions
    # ------------------------------------------------------------------

    def list(
        self,
        *,
        status: str | None = None,
        template_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Notification]:
        return self._notifications.list(
            status=status, template_key=template_key, limit=limit, offset=offset
        )

    def retry(self, notification_id: int) -> Notification:
        row = self._notifications.get(notification_id)
        if row is None:
            raise NotFoundError(f"No notification with id={notification_id}")
        if row.status not in {
            NotificationStatus.FAILED.value,
            NotificationStatus.PENDING.value,
        }:
            raise ValidationError(
                f"Notification #{row.id} is {row.status}; cannot retry."
            )
        row.status = NotificationStatus.PENDING.value
        row.attempts = 0
        row.last_error = None
        self._notifications.update(row)
        if self._audit is not None:
            self._audit.record(
                actor=self._actor,
                actor_label=self._actor_label,
                source=self._source,
                entity_type=AuditEntityType.NOTIFICATION,
                entity_id=row.id,
                action=AuditAction.RETRY_NOTIFICATION,
                details={"template_key": row.template_key},
            )
        return row

    # ------------------------------------------------------------------
    # Pruning
    # ------------------------------------------------------------------

    def prune(
        self,
        *,
        older_than: datetime | None = None,
        status: str | None = None,
        dry_run: bool = False,
    ) -> int:
        if older_than is None and status is None:
            raise ValidationError(
                "Specify at least one of 'older_than' or 'status' to prune."
            )
        if status is not None:
            valid = {s.value for s in NotificationStatus}
            if status not in valid:
                raise ValidationError(
                    f"Unknown status '{status}'. Valid: {sorted(valid)}"
                )
        deleted = self._notifications.prune(
            older_than=older_than, status=status, dry_run=dry_run
        )
        if not dry_run and deleted and self._audit is not None:
            self._audit.record(
                actor=self._actor,
                actor_label=self._actor_label,
                source=self._source,
                entity_type=AuditEntityType.NOTIFICATION,
                entity_id=None,
                action=AuditAction.PRUNE_NOTIFICATIONS,
                details={
                    "deleted": deleted,
                    "older_than": older_than.isoformat() if older_than else None,
                    "status_filter": status,
                },
            )
        return deleted

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _patron_email(self, patron: Patron) -> str | None:
        """Return the effective notification email: contact_email wins; falls back to user.email."""
        if patron.contact_email:
            return patron.contact_email
        if patron.user_id is not None and patron.user is not None:
            return patron.user.email
        return None

    def _patron_can_receive(self, patron: Patron | None) -> bool:
        if patron is None:
            return False
        if not patron.is_active:
            return False
        if not patron.receive_notifications:
            return False
        if not self._patron_email(patron):
            return False
        return True

    def _insert(
        self,
        *,
        template: NotificationTemplate,
        context: dict[str, Any],
        patron: Patron,
        loan_id: int | None,
        hold_id: int | None,
        discriminator: int,
    ) -> Notification:
        try:
            subject = _render(template.value, "subject.txt", context)
            body = _render(template.value, "body.txt", context)
        except Exception as exc:
            raise ValidationError(
                f"Failed to render {template.value} template: {exc}"
            ) from exc
        # Strip the context down to JSON-friendly values (no datetime objects,
        # no ORM entities) — subject/body already pre-rendered so this is
        # audit/debugging metadata only.
        safe_ctx = {k: _json_safe(v) for k, v in context.items()}
        row = Notification(
            recipient_patron_id=patron.id,
            recipient_email=self._patron_email(patron),
            template_key=template.value,
            context=safe_ctx,
            subject=subject,
            body=body,
            status=NotificationStatus.PENDING.value,
            attempts=0,
            loan_id=loan_id,
            hold_id=hold_id,
            discriminator=discriminator,
        )
        return self._notifications.add(row)

    def _record_send_summary(self, counts: NotificationCounts) -> None:
        if self._audit is None:
            return
        self._audit.record(
            actor=self._actor,
            actor_label=self._actor_label,
            source=self._source,
            entity_type=AuditEntityType.NOTIFICATION,
            entity_id=None,
            action=AuditAction.SEND_NOTIFICATIONS,
            details={
                "sent": counts.sent,
                "failed": counts.failed,
                "cancelled": counts.cancelled,
                "skipped": counts.skipped,
            },
        )


def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return str(value)


__all__ = ["NotificationService", "NotificationCounts", "SMTPSender"]
