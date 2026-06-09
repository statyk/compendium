"""Integration tests for scan-pairing prune (repo methods + CLI command)."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from compendium.cli.commands.maintenance import app as maintenance_app
from compendium.domain.models import AppUser, ScanPairing
from compendium.repositories.sql.scan_pairing_repository import SqlScanPairingRepository


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
