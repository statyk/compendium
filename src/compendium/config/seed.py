from sqlalchemy.orm import Session

from compendium.domain.models import Branch, LoanPolicy, MediaType, PatronCategory, Role

_MEDIA_TYPES = [
    ("book", "Book"),
    ("vinyl", "Vinyl Record"),
    ("cd", "CD"),
    ("dvd", "DVD"),
    ("bluray", "Blu-ray"),
    ("vhs", "VHS"),
]

_PATRON_CATEGORIES = [
    ("adult", "Adult", True),
    ("child", "Child", False),
    ("staff", "Staff", False),
    ("teacher", "Teacher", False),
]

# Slimmed Librarian preset — day-to-day operations. Explicit list, no
# wildcard. New permissions added in future slices must be added here too
# (Administrator picks them up via "*"). System-tier perms (system.manage,
# user.manage, role.manage) intentionally omitted — those go on SystemAdmin.
_LIBRARIAN_PERMISSIONS = [
    # Catalog
    "work.view", "work.edit",
    "item.view", "item.create", "item.edit", "item.delete",
    "catalog.import",
    # Loans
    "loan.checkout", "loan.checkin",
    "loan.renew.any", "loan.renew.self",
    "loan.view.self", "loan.view.any",
    "loan.claim.self",
    # Holds
    "hold.place.self", "hold.place.any",
    "hold.view.self", "hold.view.any",
    # Fines
    "fine.manage", "fine.view.self",
    # Notifications
    "notification.manage",
    # Reports
    "report.view",
    # Labels
    "labels.generate",
    # Audit
    "audit.view",
    # Administration
    "patron.manage", "policy.edit", "branch.edit",
]

# SystemAdmin preset — IT/sysadmin seat in multi-person deployments. Manages
# users, roles, and infrastructure settings (slice C will add the settings
# UI). Intentionally not given librarian-tier perms; pair with a separate
# Librarian user in deployments where roles are split.
_SYSTEM_ADMIN_PERMISSIONS = [
    "system.manage",
    "user.manage",
    "role.manage",
    "audit.view",
    # Minimal view perms so SystemAdmin isn't staring at a blank page
    "item.view", "work.view",
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
            "loan.claim.self",
            "hold.place.self",
            "hold.view.self",
            "fine.view.self",
        ],
        True,
    ),
    (
        "Librarian",
        _LIBRARIAN_PERMISSIONS,
        True,
    ),
    (
        "SystemAdmin",
        _SYSTEM_ADMIN_PERMISSIONS,
        True,
    ),
    (
        "Administrator",
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

    existing_categories = {pc.code for pc in session.query(PatronCategory).all()}
    for code, display_name, is_default in _PATRON_CATEGORIES:
        if code not in existing_categories:
            session.add(
                PatronCategory(
                    code=code, display_name=display_name, is_default=is_default
                )
            )

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
