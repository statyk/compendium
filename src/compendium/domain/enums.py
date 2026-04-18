from enum import StrEnum


class ItemStatus(StrEnum):
    AVAILABLE = "available"
    CHECKED_OUT = "checked_out"
    ON_HOLD = "on_hold"
    LOST = "lost"
    DAMAGED = "damaged"
    WITHDRAWN = "withdrawn"


class ItemCondition(StrEnum):
    NEW = "new"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"


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
