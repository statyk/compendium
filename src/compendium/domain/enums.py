from enum import StrEnum


class ItemStatus(StrEnum):
    AVAILABLE = "available"
    CHECKED_OUT = "checked_out"
    ON_HOLD = "on_hold"
    LOST = "lost"
    DAMAGED = "damaged"
    CLAIMS_RETURNED = "claims_returned"
    WITHDRAWN = "withdrawn"


class ItemCondition(StrEnum):
    NEW = "new"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"


class LoanRestrictionReason(StrEnum):
    REFERENCE = "reference"
    IN_LIBRARY_USE = "in_library_use"
    ARCHIVE = "archive"
    STAFF_ONLY = "staff_only"
    DISPLAY = "display"
    OTHER = "other"


class HoldStatus(StrEnum):
    WAITING = "waiting"
    AVAILABLE = "available"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CreatorRole(StrEnum):
    AUTHOR = "author"
    EDITOR = "editor"
    ILLUSTRATOR = "illustrator"
    TRANSLATOR = "translator"
    DIRECTOR = "director"
    ARTIST = "artist"
    COMPOSER = "composer"
    PERFORMER = "performer"
    CONTRIBUTOR = "contributor"


class FineKind(StrEnum):
    OVERDUE = "overdue"
    LOST = "lost"
    DAMAGED = "damaged"
    PROCESSING = "processing"
    OTHER = "other"


class FineStatus(StrEnum):
    OUTSTANDING = "outstanding"
    PAID = "paid"
    WAIVED = "waived"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NotificationTemplate(StrEnum):
    HOLD_READY = "hold_ready"
    DUE_SOON = "due_soon"
    OVERDUE = "overdue"
