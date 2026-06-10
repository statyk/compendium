"""Integration tests for scan-pairing prune (repo methods + CLI command)."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from compendium.cli.commands.maintenance import app as maintenance_app
from compendium.domain.models import AppUser, ScanEvent, ScanPairing, ScanPendingItem
from compendium.repositories.sql.scan_event_repository import SqlScanEventRepository
from compendium.repositories.sql.scan_pairing_repository import SqlScanPairingRepository
from compendium.repositories.sql.scan_pending_item_repository import SqlScanPendingItemRepository


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure_user(session) -> AppUser:
    """Return a seeded AppUser for the FK on ScanPairing.user_id."""
    user = session.query(AppUser).first()
    if user is None:
        from compendium.domain.models import Role
        role = session.query(Role).first()
        from compendium.services.auth import hash_password
        user = AppUser(
            username="test_scan_user",
            password_hash=hash_password("pw"),
            role_id=role.id,
            is_active=True,
        )
        session.add(user)
        session.flush()
    return user


def _mk_pairing(
    session,
    *,
    expires_at: datetime,
    revoked_at: datetime | None = None,
    token_hash: str,
) -> ScanPairing:
    user = _ensure_user(session)
    pairing = ScanPairing(
        token_hash=token_hash,
        user_id=user.id,
        allowed_modes=["checkout"],
        mode="checkout",
        expires_at=expires_at,
        revoked_at=revoked_at,
    )
    session.add(pairing)
    session.flush()
    return pairing


def _seed(session) -> tuple[ScanPairing, ScanPairing, ScanPairing]:
    """Seed three pairings:
    - old_expired:  expired 100 days ago (terminal, old → should be pruned)
    - old_revoked:  still within TTL window but explicitly revoked 100 days ago
                    (terminal, old → should be pruned)
    - live_recent:  expires 60 minutes from now (live → must NOT be pruned)
    """
    now = datetime.now(timezone.utc)
    old_expired = _mk_pairing(
        session,
        expires_at=now - timedelta(days=100),
        token_hash="a" * 64,
    )
    old_revoked = _mk_pairing(
        session,
        expires_at=now + timedelta(hours=1),  # not expired by time…
        revoked_at=now - timedelta(days=100),  # …but revoked long ago
        token_hash="b" * 64,
    )
    live_recent = _mk_pairing(
        session,
        expires_at=now + timedelta(hours=1),
        token_hash="c" * 64,
    )
    return old_expired, old_revoked, live_recent


# ── Repository ────────────────────────────────────────────────────────────────

def test_count_terminal_older_than_both_old_rows(session):
    _seed(session)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    count = SqlScanPairingRepository(session).count_terminal_older_than(cutoff)

    assert count == 2


def test_count_terminal_older_than_zero_when_nothing_old(session):
    _seed(session)
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)

    assert SqlScanPairingRepository(session).count_terminal_older_than(cutoff) == 0


def test_count_terminal_excludes_live_session_with_tight_cutoff(session):
    _seed(session)
    # Use a cutoff that is in the past (7 days ago). The live_recent row
    # expires_at is ~1 hour from now (well above the cutoff) and it has no
    # revoked_at, so it must not be counted.
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    count = SqlScanPairingRepository(session).count_terminal_older_than(cutoff)

    # Only the two terminal-and-old rows (expired 100 days ago / revoked 100 days ago)
    # should be included.
    assert count == 2


def test_delete_terminal_older_than_removes_only_terminal_old_rows(session):
    old_expired, old_revoked, live_recent = _seed(session)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    deleted = SqlScanPairingRepository(session).delete_terminal_older_than(cutoff)

    assert deleted == 2
    remaining = {p.id for p in session.query(ScanPairing).all()}
    assert remaining == {live_recent.id}


def test_delete_terminal_older_than_noop_when_nothing_old(session):
    _seed(session)
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)

    deleted = SqlScanPairingRepository(session).delete_terminal_older_than(cutoff)

    assert deleted == 0
    assert session.query(ScanPairing).count() == 3


# ── CLI ───────────────────────────────────────────────────────────────────────

def patch_session_scope(scope_cm):
    """Context manager that patches session_scope in the maintenance module."""
    from unittest.mock import patch
    return patch("compendium.cli.commands.maintenance.session_scope", scope_cm)


def test_cli_errors_on_zero_days(session):
    @contextmanager
    def _scope():
        yield session

    runner = CliRunner()
    with patch_session_scope(_scope):
        result = runner.invoke(maintenance_app, ["prune-scan-pairings", "--older-than-days", "0"])

    assert result.exit_code == 1
    assert "at least 1" in (result.stderr or result.output)


def test_cli_dry_run_reports_without_deleting(session):
    _seed(session)
    runner = CliRunner()

    @contextmanager
    def _scope():
        yield session

    with patch_session_scope(_scope):
        result = runner.invoke(
            maintenance_app, ["prune-scan-pairings", "--older-than-days", "7", "--dry-run"]
        )

    assert result.exit_code == 0
    assert "Would prune 2" in result.output
    assert session.query(ScanPairing).count() == 3


def test_cli_prunes_and_reports_count(session):
    _, _, live = _seed(session)
    runner = CliRunner()

    @contextmanager
    def _scope():
        yield session

    with patch_session_scope(_scope):
        result = runner.invoke(
            maintenance_app, ["prune-scan-pairings", "--older-than-days", "7"]
        )

    assert result.exit_code == 0
    assert "Pruned 2" in result.output
    remaining = {p.id for p in session.query(ScanPairing).all()}
    assert remaining == {live.id}


# ── Cascade (events + pending items) ─────────────────────────────────────────

def _mk_scan_event(session, pairing_id: int) -> ScanEvent:
    event = ScanEvent(
        pairing_id=pairing_id,
        mode="catalog",
        kind="ok",
        message="9780000000001",
    )
    session.add(event)
    session.flush()
    return event


def _mk_pending_item(
    session,
    pairing_id: int,
    *,
    status: str = "pending",
    resolved_at: datetime | None = None,
) -> ScanPendingItem:
    item = ScanPendingItem(
        pairing_id=pairing_id,
        isbn="9780000000001",
        title="Test Book",
        meta_json={},
        status=status,
        resolved_at=resolved_at,
    )
    session.add(item)
    session.flush()
    return item


def test_cascade_deletes_events_and_resolved_pending_skips_unresolved(session):
    """Prune cascades to scan_event + resolved scan_pending_item rows.

    - A terminal pairing with a ScanEvent and a *resolved* ScanPendingItem IS
      deleted, together with its event and resolved-pending rows.
    - A terminal pairing that still has an *un-resolved* (status="pending")
      ScanPendingItem is SKIPPED entirely; its pending row survives.
    """
    now = datetime.now(timezone.utc)
    ago_40 = now - timedelta(days=40)

    # Pairing 1: deletable — expired 40 days ago, has resolved pending row
    p1 = _mk_pairing(session, expires_at=ago_40, token_hash="d" * 64)
    ev1 = _mk_scan_event(session, p1.id)
    pi1 = _mk_pending_item(
        session, p1.id, status="approved", resolved_at=ago_40
    )

    # Pairing 2: NOT deletable — expired 40 days ago, but has un-resolved pending row
    p2 = _mk_pairing(session, expires_at=ago_40, token_hash="e" * 64)
    pi2 = _mk_pending_item(session, p2.id, status="pending")

    cutoff = now - timedelta(days=7)
    pairing_repo = SqlScanPairingRepository(session)
    event_repo = SqlScanEventRepository(session)
    pending_repo = SqlScanPendingItemRepository(session)

    ids = pairing_repo.terminal_deletable_ids(cutoff)

    # Only pairing 1 is deletable
    assert ids == [p1.id]

    # Execute cascade in FK-safe order
    event_repo.delete_for_pairings(ids)
    pending_repo.delete_resolved_older_than(cutoff)
    count = pairing_repo.delete_by_ids(ids)

    assert count == 1

    # Pairing 1 and its children are gone (use fresh queries to bypass identity map)
    assert session.query(ScanPairing).filter(ScanPairing.id == p1.id).count() == 0
    assert session.query(ScanEvent).filter(ScanEvent.id == ev1.id).count() == 0
    assert session.query(ScanPendingItem).filter(ScanPendingItem.id == pi1.id).count() == 0

    # Pairing 2 and its un-resolved pending row survive
    assert session.query(ScanPairing).filter(ScanPairing.id == p2.id).count() == 1
    assert session.query(ScanPendingItem).filter(ScanPendingItem.id == pi2.id).count() == 1


def test_cascade_dry_run_reports_deletable_count(session):
    """CLI --dry-run reports the count of deletable pairings (not all terminal)."""
    now = datetime.now(timezone.utc)
    ago_40 = now - timedelta(days=40)

    # Deletable: expired + resolved children only
    p1 = _mk_pairing(session, expires_at=ago_40, token_hash="f" * 64)
    _mk_pending_item(session, p1.id, status="approved", resolved_at=ago_40)

    # Not deletable: expired but has un-resolved pending
    p2 = _mk_pairing(session, expires_at=ago_40, token_hash="g" * 64)
    _mk_pending_item(session, p2.id, status="pending")

    runner = CliRunner()

    @contextmanager
    def _scope():
        yield session

    with patch_session_scope(_scope):
        result = runner.invoke(
            maintenance_app,
            ["prune-scan-pairings", "--older-than-days", "7", "--dry-run"],
        )

    assert result.exit_code == 0
    assert "Would prune 1" in result.output
    # Nothing deleted
    assert session.query(ScanPairing).filter(ScanPairing.id.in_([p1.id, p2.id])).count() == 2
