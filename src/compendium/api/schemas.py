from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class WorkSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    subtitle: str | None
    isbn: str | None
    publisher: str | None
    publication_year: int | None


class ItemDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    barcode: str
    accession_number: str
    status: str
    location: str | None
    condition: str | None


class CreatePatronRequest(BaseModel):
    full_name: str
    contact_email: str | None = None
    contact_phone: str | None = None


class PatronResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    library_card_number: str
    full_name: str
    contact_email: str | None
    contact_phone: str | None
    is_active: bool


class CheckoutRequest(BaseModel):
    barcode: str
    card_number: str


class LoanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    item_id: int
    patron_id: int
    checked_out_at: datetime
    due_at: datetime
    returned_at: datetime | None
    renewal_count: int


class CreateHoldRequest(BaseModel):
    work_id: int
    card_number: str


class SelfHoldRequest(BaseModel):
    work_id: int


class HoldResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    work_id: int
    patron_id: int
    status: str
    placed_at: datetime
    expires_at: datetime | None


class CreatePolicyRequest(BaseModel):
    name: str
    media_type_id: int | None = None
    loan_period_days: int
    max_renewals: int = 2
    is_default: bool = False


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None
    is_active: bool


class LoanPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    media_type_id: int | None
    loan_period_days: int
    max_renewals: int
    is_default: bool


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_at: datetime
    user_id: int | None
    actor_label: str | None
    source: str
    entity_type: str
    entity_id: int | None
    action: str
    details: dict | None
