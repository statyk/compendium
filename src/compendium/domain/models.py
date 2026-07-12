from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, Index, Integer, String, Text, Time, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import expression
from sqlalchemy.types import JSON

from compendium.domain.enums import HoldStatus, ItemNoteKind, ItemStatus
from compendium.domain.types import UtcDateTime


class Base(DeclarativeBase):
    pass


class MediaType(Base):
    __tablename__ = "media_type"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(64))


class Branch(Base):
    __tablename__ = "branch"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    address: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    default_classification_scheme: Mapped[str] = mapped_column(
        String(8), default="none", server_default="none"
    )
    location_code: Mapped[str | None] = mapped_column(String(4), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())


class Creator(Base):
    __tablename__ = "creator"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(256))
    sort_name: Mapped[str] = mapped_column(String(256), index=True)
    external_ids: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())


class WorkCreator(Base):
    __tablename__ = "work_creator"

    work_id: Mapped[int] = mapped_column(
        ForeignKey("work.id", ondelete="CASCADE"), primary_key=True
    )
    creator_id: Mapped[int] = mapped_column(ForeignKey("creator.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    work: Mapped[Work] = relationship(back_populates="creators")
    creator: Mapped[Creator] = relationship()


class Work(Base):
    __tablename__ = "work"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    subtitle: Mapped[str | None] = mapped_column(String(512))
    media_type_id: Mapped[int] = mapped_column(ForeignKey("media_type.id"))
    publisher: Mapped[str | None] = mapped_column(String(256))
    publication_year: Mapped[int | None] = mapped_column(Integer)
    edition: Mapped[str | None] = mapped_column(String(128))
    language: Mapped[str | None] = mapped_column(String(8))
    description: Mapped[str | None] = mapped_column(Text)
    isbn: Mapped[str | None] = mapped_column(String(13), index=True)
    upc: Mapped[str | None] = mapped_column(String(20), index=True)
    classification_scheme: Mapped[str | None] = mapped_column(String(32))
    classification_code: Mapped[str | None] = mapped_column(String(64))
    cover_image_url: Mapped[str | None] = mapped_column(String(512))
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    external_ids: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    search_text: Mapped[str | None] = mapped_column(Text)
    sort_title: Mapped[str] = mapped_column(String(512), index=True, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, onupdate=func.now()
    )

    media_type: Mapped[MediaType] = relationship()
    creators: Mapped[list[WorkCreator]] = relationship(
        back_populates="work", cascade="all, delete-orphan", order_by="WorkCreator.display_order"
    )
    items: Mapped[list[Item]] = relationship(back_populates="work")


class Item(Base):
    __tablename__ = "item"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("work.id"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branch.id"))
    barcode: Mapped[str] = mapped_column(String(64), unique=True)
    accession_number: Mapped[str] = mapped_column(String(64), unique=True)
    call_number: Mapped[str | None] = mapped_column(String(64))
    location: Mapped[str | None] = mapped_column(String(256))
    condition: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=ItemStatus.AVAILABLE.value, index=True)
    is_loanable: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    loan_restriction_reason: Mapped[str | None] = mapped_column(String(32))
    loan_restriction_note: Mapped[str | None] = mapped_column(String(256))
    acquired_at: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, onupdate=func.now()
    )

    work: Mapped[Work] = relationship(back_populates="items")
    branch: Mapped[Branch] = relationship()
    note_entries: Mapped[list["ItemNote"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        order_by="ItemNote.created_at.desc()",
    )


class ItemNote(Base):
    __tablename__ = "item_note"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("item.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), default=ItemNoteKind.GENERAL.value, server_default="general")
    note: Mapped[str] = mapped_column(Text)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id"), nullable=True
    )
    actor_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())

    item: Mapped["Item"] = relationship(back_populates="note_entries")
    author: Mapped["AppUser | None"] = relationship(foreign_keys=[user_id])


class Role(Base):
    __tablename__ = "role"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())

    users: Mapped[list[AppUser]] = relationship(back_populates="role")


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str | None] = mapped_column(String(256))
    password_hash: Mapped[str] = mapped_column(String(256))
    role_id: Mapped[int] = mapped_column(ForeignKey("role.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, onupdate=func.now()
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    role: Mapped[Role] = relationship(back_populates="users")
    patron: Mapped[Patron | None] = relationship(
        "Patron",
        foreign_keys="[Patron.user_id]",
        back_populates="user",
        uselist=False,
    )


class CuratedList(Base):
    __tablename__ = "curated_list"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(96), unique=True)
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(UtcDateTime, onupdate=func.now())
    entries: Mapped[list["CuratedListEntry"]] = relationship(
        back_populates="curated_list",
        cascade="all, delete-orphan",
        order_by="CuratedListEntry.display_order",
    )


class CuratedListEntry(Base):
    __tablename__ = "curated_list_entry"

    list_id: Mapped[int] = mapped_column(
        ForeignKey("curated_list.id", ondelete="CASCADE"), primary_key=True
    )
    work_id: Mapped[int] = mapped_column(
        ForeignKey("work.id", ondelete="CASCADE"), primary_key=True
    )
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    annotation: Mapped[str | None] = mapped_column(Text, nullable=True)

    curated_list: Mapped["CuratedList"] = relationship(back_populates="entries")
    work: Mapped["Work"] = relationship()


class Household(Base):
    __tablename__ = "household"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(UtcDateTime, onupdate=func.now())

    members: Mapped[list["Patron"]] = relationship(
        "Patron",
        foreign_keys="[Patron.household_id]",
        back_populates="household",
    )


class PatronCategory(Base):
    __tablename__ = "patron_category"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(64))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())


class Patron(Base):
    __tablename__ = "patron"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("patron_category.id"), nullable=True, index=True
    )
    library_card_number: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(256))
    contact_email: Mapped[str | None] = mapped_column(String(256))
    contact_phone: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    expires_at: Mapped[date | None] = mapped_column(Date)
    receive_notifications: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )
    household_id: Mapped[int | None] = mapped_column(
        ForeignKey("household.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, onupdate=func.now()
    )

    user: Mapped[AppUser | None] = relationship(foreign_keys=[user_id], back_populates="patron")
    category: Mapped[PatronCategory | None] = relationship(foreign_keys=[category_id])
    household: Mapped["Household | None"] = relationship(
        "Household",
        foreign_keys=[household_id],
        back_populates="members",
    )


class Loan(Base):
    __tablename__ = "loan"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id"), index=True)
    patron_id: Mapped[int] = mapped_column(ForeignKey("patron.id"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branch.id"))
    checked_out_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now()
    )
    due_at: Mapped[datetime] = mapped_column(UtcDateTime)
    returned_at: Mapped[datetime | None] = mapped_column(UtcDateTime, index=True)
    renewal_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    notes: Mapped[str | None] = mapped_column(Text)

    item: Mapped[Item] = relationship()
    patron: Mapped[Patron] = relationship()
    branch: Mapped[Branch] = relationship()


class LoanPolicy(Base):
    __tablename__ = "loan_policy"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    media_type_id: Mapped[int | None] = mapped_column(ForeignKey("media_type.id"), nullable=True)
    patron_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("patron_category.id"), nullable=True, index=True
    )
    loan_period_days: Mapped[int] = mapped_column(Integer)
    max_renewals: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    overdue_fine_per_day_cents: Mapped[int | None] = mapped_column(Integer)
    overdue_fine_cap_cents: Mapped[int | None] = mapped_column(Integer)
    grace_period_days: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    lost_item_default_cents: Mapped[int | None] = mapped_column(Integer)
    lost_item_processing_fee_cents: Mapped[int | None] = mapped_column(Integer)

    media_type: Mapped[MediaType | None] = relationship()
    patron_category: Mapped[PatronCategory | None] = relationship()


class Hold(Base):
    __tablename__ = "hold"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("work.id"), index=True)
    patron_id: Mapped[int] = mapped_column(ForeignKey("patron.id"), index=True)
    branch_id: Mapped[int] = mapped_column(ForeignKey("branch.id"))
    status: Mapped[str] = mapped_column(String(16), default=HoldStatus.WAITING.value, index=True)
    placed_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    notified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    held_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("item.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Suspend/freeze: patron can temporarily park a WAITING hold so the queue
    # skips over them. NULL suspended_until = "indefinite" (resume manually);
    # a date value means auto-resume when the date passes. suspended_reason
    # is optional free-text, surfaced in the UI as context.
    suspended_until: Mapped[date | None] = mapped_column(Date)
    suspended_reason: Mapped[str | None] = mapped_column(String(256))

    work: Mapped[Work] = relationship()
    patron: Mapped[Patron] = relationship()
    branch: Mapped[Branch] = relationship()
    held_item: Mapped[Item | None] = relationship(foreign_keys=[held_item_id])


class Fine(Base):
    __tablename__ = "fine"
    __table_args__ = (
        Index("ix_fine_patron_status", "patron_id", "status"),
        Index("ix_fine_loan", "loan_id"),
        Index(
            "ix_fine_overdue_uniq",
            "loan_id",
            unique=True,
            sqlite_where=text("status = 'outstanding' AND kind = 'overdue'"),
            postgresql_where=text("status = 'outstanding' AND kind = 'overdue'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    patron_id: Mapped[int] = mapped_column(ForeignKey("patron.id"), index=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loan.id"), nullable=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("item.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    assessed_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    reason: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    resolved_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id"), nullable=True
    )

    patron: Mapped[Patron] = relationship()
    loan: Mapped[Loan | None] = relationship()
    item: Mapped[Item | None] = relationship()
    resolved_by: Mapped[AppUser | None] = relationship(foreign_keys=[resolved_by_user_id])


class Notification(Base):
    __tablename__ = "notification"
    __table_args__ = (
        Index("ix_notification_status", "status"),
        Index(
            "ix_notification_scheduled",
            "scheduled_for",
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_notification_loan_dedup",
            "loan_id",
            "template_key",
            "discriminator",
            unique=True,
            sqlite_where=text("loan_id IS NOT NULL AND status != 'cancelled'"),
            postgresql_where=text("loan_id IS NOT NULL AND status != 'cancelled'"),
        ),
        Index(
            "ix_notification_hold_dedup",
            "hold_id",
            "template_key",
            "discriminator",
            unique=True,
            sqlite_where=text("hold_id IS NOT NULL AND status != 'cancelled'"),
            postgresql_where=text("hold_id IS NOT NULL AND status != 'cancelled'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    recipient_patron_id: Mapped[int | None] = mapped_column(
        ForeignKey("patron.id"), nullable=True
    )
    recipient_email: Mapped[str | None] = mapped_column(String(256))
    template_key: Mapped[str] = mapped_column(String(32))
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loan.id"), nullable=True)
    hold_id: Mapped[int | None] = mapped_column(ForeignKey("hold.id"), nullable=True)
    discriminator: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    scheduled_for: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())

    patron: Mapped[Patron | None] = relationship()
    loan: Mapped[Loan | None] = relationship()
    hold: Mapped[Hold | None] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id", "occurred_at"),
        Index("ix_audit_log_user_time", "user_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), index=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    actor_label: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(16))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    actor: Mapped[AppUser | None] = relationship()


class DeletedEntity(Base):
    """Trash row: a JSON snapshot of a hard-deleted entity and its children.

    Live tables only ever contain live rows; recoverability lives here.
    """

    __tablename__ = "deleted_entity"
    __table_args__ = (
        Index("ix_deleted_entity_type_deleted_at", "entity_type", "deleted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(512))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    deleted_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    deleted_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)

    deleted_by_user: Mapped["AppUser | None"] = relationship()


class SiteSetting(Base):
    __tablename__ = "site_setting"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now()
    )
    updated_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )

    updated_by: Mapped[AppUser | None] = relationship()


class Counter(Base):
    __tablename__ = "counters"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)


class FailedLogin(Base):
    """One row per failed authentication attempt.

    Keyed on (scope, identifier) with occurred_at for sliding-window queries.
    No FK to app_user or patron — a non-existent username still counts.
    """

    __tablename__ = "failed_login"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    identifier: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        Index("ix_failed_login_scope_id_at", "scope", "identifier", "occurred_at"),
    )


class LibraryHours(Base):
    """Open/close schedule for each weekday (0 = Monday … 6 = Sunday, ISO convention)."""

    __tablename__ = "library_hours"

    weekday: Mapped[int] = mapped_column(Integer, primary_key=True)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    open_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    close_time: Mapped[time | None] = mapped_column(Time, nullable=True)


class ClosedDate(Base):
    """A date range during which the library is closed.

    ``start_date`` and ``end_date`` are inclusive local dates.
    When ``recurs_annually`` is True the closure repeats on the same
    month/day every year (the stored year is the anchor, not a limit).
    """

    __tablename__ = "closed_date"

    id: Mapped[int] = mapped_column(primary_key=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recurs_annually: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )


class MetadataCache(Base):
    """Persistent cache for external metadata lookups (Google Books, Open Library, MusicBrainz, TMDb, etc.).

    Keyed on (adapter, kind, lookup_value) so adapter-source switching never
    causes cross-namespace pollution. payload is JSON-encoded; NULL on
    is_negative rows. fetched_at is indexed for the prune maintenance command.
    """

    __tablename__ = "metadata_cache"

    adapter: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    lookup_value: Mapped[str] = mapped_column(String(255), primary_key=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_negative: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, index=True)


class ScanPairing(Base):
    """A staff member's paired phone-scanner session.

    Created when a staff member generates a pairing QR (pre-claim), then
    upgraded in place when a phone claims it. We store only the SHA-256 hex
    digest of the *current* secret (the claim secret before claim, the session
    secret after) — never the raw secret. Lookups hash the presented secret and
    match against ``token_hash``.

    ``mode`` is the current scanner mode (one of checkout/checkin/catalog) and
    ``allowed_modes`` is the subset the staff member permitted at pairing time.
    ``count`` tracks items handled this session; ``borrower_patron_id`` is the
    current borrower in checkout mode.
    """

    __tablename__ = "scan_pairing"
    __table_args__ = (
        Index("ix_scan_pairing_token_hash", "token_hash", unique=True),
        Index("ix_scan_pairing_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    allowed_modes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    borrower_patron_id: Mapped[int | None] = mapped_column(
        ForeignKey("patron.id"), nullable=True
    )
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    catalog_review: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=expression.false(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    user: Mapped[AppUser] = relationship()
    borrower: Mapped[Patron | None] = relationship()


class ScanEvent(Base):
    """One row per non-ignored phone-scanner dispatch — the desk live feed log.

    Append-only and ephemeral; pruned alongside terminal pairings. ``kind`` is
    ``"ok"`` or ``"error"`` (the dispatch reply's ok flag); the 2-second
    idempotency collapse (``kind == "ignored"``) is never written.
    """

    __tablename__ = "scan_event"
    __table_args__ = (Index("ix_scan_event_pairing_id", "pairing_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    pairing_id: Mapped[int] = mapped_column(
        ForeignKey("scan_pairing.id"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(String(255), nullable=False)
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("item.id"), nullable=True
    )
    patron_id: Mapped[int | None] = mapped_column(
        ForeignKey("patron.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now()
    )

    item: Mapped[Item | None] = relationship()


class ScanPendingItem(Base):
    """A catalog scan held for desk review (review-first mode).

    Created when an ISBN is scanned while the pairing's ``catalog_review`` flag
    is on. No Item exists yet; ``meta_json`` is the fetched metadata snapshot
    used to create the Work+Item on approve.
    """

    __tablename__ = "scan_pending_item"
    __table_args__ = (Index("ix_scan_pending_item_pairing_id", "pairing_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    pairing_id: Mapped[int] = mapped_column(
        ForeignKey("scan_pairing.id"), nullable=False
    )
    isbn: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    meta_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    cover_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id"), nullable=True
    )
    created_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("item.id"), nullable=True
    )
