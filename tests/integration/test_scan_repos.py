"""Repos for scan_event (feed) and scan_pending_item (review queue)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from compendium.domain.models import AppUser, ScanEvent, ScanPairing, ScanPendingItem
from compendium.repositories.sql.scan_event_repository import SqlScanEventRepository
from compendium.repositories.sql.scan_pending_item_repository import (
    SqlScanPendingItemRepository,
)


def _ensure_user(session) -> AppUser:
    """Return a seeded AppUser for the FK on ScanPairing.user_id."""
    user = session.query(AppUser).first()
    if user is None:
        from compendium.domain.models import Role
        from compendium.services.auth import hash_password

        role = session.query(Role).first()
        user = AppUser(
            username="test_scan_repo_user",
            password_hash=hash_password("pw"),
            role_id=role.id,
            is_active=True,
        )
        session.add(user)
        session.flush()
    return user


def _pairing(session, user, modes=("catalog",)) -> ScanPairing:
    now = datetime.now(timezone.utc)
    p = ScanPairing(
        token_hash="y" * 64, user_id=user.id, allowed_modes=list(modes),
        mode=modes[0], count=0, created_at=now,
        expires_at=now + timedelta(minutes=60), claimed_at=now,
    )
    session.add(p)
    session.flush()
    return p


def test_event_recent_for_pairing_newest_first(session):
    user = _ensure_user(session)
    p = _pairing(session, user)
    repo = SqlScanEventRepository(session)
    for i in range(3):
        repo.add(ScanEvent(pairing_id=p.id, mode="catalog", kind="ok",
                           message=f"msg {i}"))
    recent = repo.recent_for_pairing(p.id, limit=2)
    assert [e.message for e in recent] == ["msg 2", "msg 1"]


def test_pending_lists_only_pending(session):
    user = _ensure_user(session)
    p = _pairing(session, user)
    repo = SqlScanPendingItemRepository(session)
    a = repo.add(ScanPendingItem(pairing_id=p.id, isbn="1", title="A",
                                 meta_json={}, status="pending"))
    repo.add(ScanPendingItem(pairing_id=p.id, isbn="2", title="B",
                             meta_json={}, status="discarded"))
    pending = repo.pending_for_user(user.id)
    assert [x.title for x in pending] == ["A"]
    assert repo.get(a.id).title == "A"


def test_prune_resolved_and_events(session):
    user = _ensure_user(session)
    p = _pairing(session, user)
    SqlScanEventRepository(session).add(
        ScanEvent(pairing_id=p.id, mode="catalog", kind="ok", message="x")
    )
    pend_repo = SqlScanPendingItemRepository(session)
    resolved = pend_repo.add(ScanPendingItem(
        pairing_id=p.id, isbn="1", title="A", meta_json={}, status="approved",
        resolved_at=datetime.now(timezone.utc) - timedelta(days=40),
    ))
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    assert SqlScanEventRepository(session).delete_for_pairings([p.id]) == 1
    assert pend_repo.delete_resolved_older_than(cutoff) == 1
    assert pend_repo.get(resolved.id) is None
