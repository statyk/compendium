from __future__ import annotations

from datetime import date, datetime

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
    is_loanable: bool
    loan_restriction_reason: str | None
    loan_restriction_note: str | None


class WorkDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    subtitle: str | None
    publisher: str | None
    publication_year: int | None
    edition: str | None
    language: str | None
    description: str | None
    isbn: str | None
    upc: str | None
    classification_scheme: str | None
    classification_code: str | None
    cover_image_url: str | None


class WorkUpdate(BaseModel):
    # None means "clear this field"; an omitted field means "leave it".
    # ISBN, UPC, media type, creators, and raw external metadata are not
    # editable here — those need a merge/split flow that v1 doesn't ship.
    title: str | None = None
    subtitle: str | None = None
    publisher: str | None = None
    publication_year: int | None = None
    edition: str | None = None
    language: str | None = None
    description: str | None = None
    classification_scheme: str | None = None
    classification_code: str | None = None
    cover_image_url: str | None = None


class CreatorInput(BaseModel):
    name: str
    role: str


class WorkCreatorsReplace(BaseModel):
    creators: list[CreatorInput]


class CreatorSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str
    sort_name: str


class CreatorRename(BaseModel):
    display_name: str


class ItemUpdate(BaseModel):
    # None means "clear this field"; an omitted field means "leave it".
    # Fields outside this set (barcode, accession, status, work_id, branch_id)
    # are intentionally not editable via PATCH.
    location: str | None = None
    call_number: str | None = None
    condition: str | None = None
    notes: str | None = None


class LoanableUpdate(BaseModel):
    is_loanable: bool
    reason: str | None = None
    note: str | None = None


class CreatePatronRequest(BaseModel):
    full_name: str
    contact_email: str | None = None
    contact_phone: str | None = None
    category_code: str | None = None
    expires_at: date | None = None


class UpdatePatronRequest(BaseModel):
    # None means "clear"; omitted means "leave it" (handled by model_fields_set).
    category_code: str | None = None
    expires_at: date | None = None


class PatronResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    library_card_number: str
    full_name: str
    contact_email: str | None
    contact_phone: str | None
    is_active: bool
    category_id: int | None = None
    expires_at: date | None = None


class PatronCategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    display_name: str
    is_default: bool


class CreatePatronCategoryRequest(BaseModel):
    code: str
    display_name: str
    is_default: bool = False


class UpdatePatronCategoryRequest(BaseModel):
    display_name: str | None = None
    is_default: bool | None = None


class CheckoutRequest(BaseModel):
    barcode: str
    card_number: str
    override_holds: bool = False


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
    held_item_id: int | None = None


class CreatePolicyRequest(BaseModel):
    name: str
    media_type_id: int | None = None
    patron_category_id: int | None = None
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
    patron_category_id: int | None = None
    loan_period_days: int
    max_renewals: int
    is_default: bool


class BranchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    is_default: bool
    default_classification_scheme: str


class UpdateBranchRequest(BaseModel):
    default_classification_scheme: str


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


class ImportRowErrorResponse(BaseModel):
    row_number: int
    identifier: str
    message: str


class ImportReportResponse(BaseModel):
    source: str
    filename: str | None
    total_rows: int
    created_works: int
    added_copies: int
    skipped_duplicates: int
    errors: list[ImportRowErrorResponse]
    dry_run: bool


class FineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    patron_id: int
    loan_id: int | None
    item_id: int | None
    kind: str
    amount_cents: int
    status: str
    assessed_at: datetime
    resolved_at: datetime | None
    reason: str | None
    note: str | None


class AssessManualFineRequest(BaseModel):
    patron_card: str
    kind: str
    amount_cents: int
    note: str | None = None
    reason: str | None = None
    loan_id: int | None = None
    item_id: int | None = None


class WaiveFineRequest(BaseModel):
    note: str


class DeclareLostRequest(BaseModel):
    replacement_cost_cents: int | None = None
    note: str | None = None


class MarkDamagedRequest(BaseModel):
    amount_cents: int
    note: str


class AssessOverdueResponse(BaseModel):
    created: int
    updated: int
    unchanged: int


class MonthlyCheckoutsResponse(BaseModel):
    month: str
    count: int


class PopularWorkResponse(BaseModel):
    work_id: int
    title: str
    subtitle: str | None
    media_type_code: str
    checkout_count: int


class DormantItemResponse(BaseModel):
    item_id: int
    barcode: str
    title: str
    media_type_code: str
    branch_code: str
    last_checkout_at: datetime | None


class OverdueLoanResponse(BaseModel):
    loan_id: int
    patron_card: str
    patron_name: str
    item_barcode: str
    title: str
    due_at: datetime
    days_overdue: int


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    recipient_patron_id: int | None
    recipient_email: str | None
    template_key: str
    status: str
    attempts: int
    last_error: str | None
    subject: str
    scheduled_for: datetime
    sent_at: datetime | None
    created_at: datetime
