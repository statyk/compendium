from sqlalchemy.orm import Session

from compendium.domain.models import Branch, LoanPolicy, MediaType, Role

_MEDIA_TYPES = [
    ("book", "Book"),
    ("vinyl", "Vinyl Record"),
    ("dvd", "DVD"),
    ("cd", "CD"),
]

_PRESET_ROLES = [
    (
        "ReadOnly",
        ["item.view", "work.view"],
        True,
    ),
    (
        "Patron",
        [
            "item.view",
            "work.view",
            "loan.view.self",
            "loan.renew.self",
            "hold.place.self",
            "hold.view.self",
        ],
        True,
    ),
    (
        "Librarian",
        ["*"],
        True,
    ),
]


def seed_defaults(session: Session) -> None:
    """Insert default branch, media types, and preset roles if not already present."""
    if not session.query(Branch).filter_by(is_default=True).first():
        session.add(Branch(code="MAIN", name="Main Collection", is_default=True))

    existing_codes = {mt.code for mt in session.query(MediaType).all()}
    for code, display_name in _MEDIA_TYPES:
        if code not in existing_codes:
            session.add(MediaType(code=code, display_name=display_name))

    existing_roles = {r.name for r in session.query(Role).all()}
    for name, permissions, is_system in _PRESET_ROLES:
        if name not in existing_roles:
            session.add(Role(name=name, permissions=permissions, is_system=is_system))

    if not session.query(LoanPolicy).filter_by(is_default=True).first():
        session.add(
            LoanPolicy(
                name="Default",
                media_type_id=None,
                loan_period_days=14,
                max_renewals=2,
                is_default=True,
            )
        )

    session.flush()
