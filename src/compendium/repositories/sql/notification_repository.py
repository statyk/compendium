from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from compendium.domain.enums import NotificationStatus
from compendium.domain.models import Notification


class SqlNotificationRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, notification: Notification) -> Notification:
        self._s.add(notification)
        self._s.flush()
        return notification

    def get(self, notification_id: int) -> Notification | None:
        return self._s.get(Notification, notification_id)

    def list(
        self,
        *,
        status: str | None = None,
        template_key: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Notification]:
        q = self._s.query(Notification)
        if status is not None:
            q = q.filter(Notification.status == status)
        if template_key is not None:
            q = q.filter(Notification.template_key == template_key)
        return (
            q.order_by(Notification.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def list_pending(self, *, limit: int) -> list[Notification]:
        return (
            self._s.query(Notification)
            .filter(Notification.status == NotificationStatus.PENDING.value)
            .order_by(Notification.scheduled_for)
            .limit(limit)
            .all()
        )

    def get_existing(
        self,
        *,
        loan_id: int | None,
        hold_id: int | None,
        template_key: str,
        discriminator: int,
    ) -> Notification | None:
        q = self._s.query(Notification).filter(
            Notification.template_key == template_key,
            Notification.discriminator == discriminator,
            Notification.status != NotificationStatus.CANCELLED.value,
        )
        if loan_id is not None:
            q = q.filter(Notification.loan_id == loan_id)
        elif hold_id is not None:
            q = q.filter(Notification.hold_id == hold_id)
        else:
            return None
        return q.first()

    def update(self, notification: Notification) -> Notification:
        self._s.flush()
        return notification

    def prune(
        self,
        *,
        older_than: datetime | None = None,
        status: str | None = None,
        dry_run: bool = False,
    ) -> int:
        q = self._s.query(Notification)
        if older_than is not None:
            q = q.filter(Notification.created_at < older_than)
        if status is not None:
            q = q.filter(Notification.status == status)
        else:
            # Age-only prune defaults to sent + cancelled (preserve failed).
            if older_than is not None:
                q = q.filter(
                    Notification.status.in_(
                        [
                            NotificationStatus.SENT.value,
                            NotificationStatus.CANCELLED.value,
                        ]
                    )
                )
        count = q.count()
        if not dry_run and count:
            q.delete(synchronize_session=False)
            self._s.flush()
        return count
