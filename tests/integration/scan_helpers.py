"""Shared plain helpers for the /ui/scan/* integration tests.

Fixtures live in ``tests/integration/conftest.py`` (auto-discovered by pytest);
this module holds the pure helper functions that the scan suites import.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from compendium.domain.identifiers import format_item_barcode, format_patron_card
from compendium.domain.models import (
    AppUser,
    Item,
    MediaType,
    Patron,
    Role,
    ScanPairing,
    Work,
)
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.role_repository import SqlRoleRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.services.auth import hash_password
from compendium.web.csrf import _COOKIE as CSRF_COOKIE
from compendium.web.csrf import _derive_csrf_secret, _sign, generate_token
from compendium.web.deps import SCAN_COOKIE

_SECRET = "insecure-default-change-in-production"

# A monotonically increasing counter for unique barcodes/usernames/cards across
# the whole scan suite. Shared so collisions can't happen between files.
_n = {"i": 0}


def next_id() -> int:
    _n["i"] += 1
    return _n["i"]


def csrf_pair() -> tuple[str, str]:
    raw = generate_token()
    return raw, f"{raw}.{_sign(raw, _derive_csrf_secret(_SECRET))}"


def staff_user(session, role_name="Librarian", *, prefix="scanu") -> AppUser:
    role = SqlRoleRepository(session).get_by_name(role_name)
    u = AppUser(
        username=f"{prefix}{next_id()}",
        password_hash=hash_password("x"),
        role_id=role.id,
    )
    SqlUserRepository(session).add(u)
    session.flush()
    return u


def custom_role(session, name, permissions) -> Role:
    role = Role(name=name, permissions=permissions, is_system=False)
    session.add(role)
    session.flush()
    return role


def make_pairing(
    session, user, *, claim, allowed_modes, mode=None, ttl_minutes=2
) -> ScanPairing:
    now = datetime.now(timezone.utc)
    row = ScanPairing(
        token_hash=hashlib.sha256(claim.encode()).hexdigest(),
        user_id=user.id,
        allowed_modes=allowed_modes,
        mode=mode or allowed_modes[0],
        count=0,
        created_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
    )
    session.add(row)
    session.flush()
    return row


def claim(client, session, user, *, allowed_modes, mode=None):
    """Manufacture a pairing with a known claim secret, claim it via the route.

    Returns ``(row, scan_cookie)``.
    """
    secret = f"CLAIM_{next_id()}"
    row = make_pairing(
        session, user, claim=secret, allowed_modes=allowed_modes, mode=mode
    )
    resp = client.get(f"/ui/scan/pair?c={secret}")
    assert resp.status_code == 200
    return row, resp.cookies[SCAN_COOKIE]


def login(client, session, *, role_name="Librarian", username=None) -> dict:
    username = username or f"scanstaff{next_id()}"
    role = SqlRoleRepository(session).get_by_name(role_name)
    user = AppUser(
        username=username, password_hash=hash_password("secret"), role_id=role.id
    )
    SqlUserRepository(session).add(user)
    session.flush()
    raw, signed = csrf_pair()
    resp = client.post(
        "/ui/login",
        data={"username": username, "password": "secret", "csrf_token": raw},
        cookies={CSRF_COOKIE: signed},
    )
    assert resp.status_code == 303
    return dict(resp.cookies)


def create_pairing(client, cookies, *, modes=("checkout", "checkin", "catalog")):
    raw, signed = csrf_pair()
    cookies = {**cookies, CSRF_COOKIE: signed}
    data = dict.fromkeys(modes, "on")
    data["csrf_token"] = raw
    return client.post("/ui/scan/pairings", data=data, cookies=cookies)


def book(session, title="Dune", *, acc_prefix="SACC") -> Item:
    mt = session.query(MediaType).filter_by(code="book").one()
    w = Work(title=title, media_type_id=mt.id)
    session.add(w)
    session.flush()
    branch = SqlBranchRepository(session).get_default()
    n = next_id()
    it = Item(
        work_id=w.id,
        branch_id=branch.id,
        barcode=format_item_barcode(f"{n:08d}", location_code=None),
        accession_number=f"{acc_prefix}{n:06d}",
    )
    session.add(it)
    session.flush()
    return it


def patron(session, *, name_prefix="Scan Patron") -> Patron:
    n = next_id()
    p = Patron(
        library_card_number=format_patron_card(f"{n:08d}", location_code=None),
        full_name=f"{name_prefix} {n}",
        is_active=True,
    )
    session.add(p)
    session.flush()
    return p
