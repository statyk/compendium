"""ScanEvent / ScanPendingItem persistence + the catalog_review column."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from compendium.domain.models import (
    AppUser,
    ScanEvent,
    ScanPairing,
    ScanPendingItem,
)


def _ensure_user(session) -> AppUser:
    """Return a seeded AppUser for the FK on ScanPairing.user_id."""
    user = session.query(AppUser).first()
    if user is None:
        from compendium.domain.models import Role
        from compendium.services.auth import hash_password

        role = session.query(Role).first()
        user = AppUser(
            username="test_scan_user",
            password_hash=hash_password("pw"),
            role_id=role.id,
            is_active=True,
        )
        session.add(user)
        session.flush()
    return user


def _pairing(session) -> ScanPairing:
    user = _ensure_user(session)
    now = datetime.now(timezone.utc)
    p = ScanPairing(
        token_hash="x" * 64, user_id=user.id, allowed_modes=["catalog"],
        mode="catalog", count=0, created_at=now,
        expires_at=now + timedelta(minutes=60), claimed_at=now,
    )
    session.add(p)
    session.flush()
    return p


def test_catalog_review_defaults_false(session):
    p = _pairing(session)
    assert p.catalog_review is False


def test_scan_event_roundtrip(session):
    p = _pairing(session)
    ev = ScanEvent(pairing_id=p.id, mode="checkout", kind="ok",
                   message="Checked out: The Hobbit")
    session.add(ev)
    session.flush()
    assert ev.id is not None
    assert ev.created_at is not None
    assert ev.item_id is None


def test_scan_pending_item_roundtrip(session):
    p = _pairing(session)
    pend = ScanPendingItem(
        pairing_id=p.id, isbn="9780261103344", title="The Hobbit",
        meta_json={"title": "The Hobbit", "authors": ["J.R.R. Tolkien"]},
        cover_url=None, status="pending",
    )
    session.add(pend)
    session.flush()
    assert pend.id is not None
    assert pend.status == "pending"
    assert pend.meta_json["authors"] == ["J.R.R. Tolkien"]
    assert pend.resolved_at is None
