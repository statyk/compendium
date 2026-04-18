from sqlalchemy.orm import Session

from compendium.domain.models import Branch, MediaType

_MEDIA_TYPES = [
    ("book", "Book"),
    ("vinyl", "Vinyl Record"),
    ("dvd", "DVD"),
    ("cd", "CD"),
]


def seed_defaults(session: Session) -> None:
    """Insert default branch and media types if they are not already present."""
    if not session.query(Branch).filter_by(is_default=True).first():
        session.add(Branch(code="MAIN", name="Main Collection", is_default=True))

    existing_codes = {mt.code for mt in session.query(MediaType).all()}
    for code, display_name in _MEDIA_TYPES:
        if code not in existing_codes:
            session.add(MediaType(code=code, display_name=display_name))

    session.flush()
