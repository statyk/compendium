from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from compendium.domain.enums import HoldStatus, ItemStatus
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
    acquired_at: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, onupdate=func.now()
    )

    work: Mapped[Work] = relationship(back_populates="items")
    branch: Mapped[Branch] = relationship()


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

    role: Mapped[Role] = relationship(back_populates="users")


class Patron(Base):
    __tablename__ = "patron"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    library_card_number: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(256))
    contact_email: Mapped[str | None] = mapped_column(String(256))
    contact_phone: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime, onupdate=func.now()
    )

    user: Mapped[AppUser | None] = relationship(foreign_keys=[user_id])


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
    loan_period_days: Mapped[int] = mapped_column(Integer)
    max_renewals: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    media_type: Mapped[MediaType | None] = relationship()


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

    work: Mapped[Work] = relationship()
    patron: Mapped[Patron] = relationship()
    branch: Mapped[Branch] = relationship()


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
