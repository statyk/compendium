"""Integration tests for audit log retention/prune."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from typer.testing import CliRunner

from compendium.cli.commands.maintenance import app as maintenance_app
from compendium.config.settings import Settings
from compendium.domain.models import AuditLog
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository


def _mk_entry(session, *, when: datetime, entity_id: int = 1) -> AuditLog:
    entry = AuditLog(
        occurred_at=when,
        user_id=None,
        actor_label="test",
        source="test",
        entity_type="work",
        entity_id=entity_id,
        action="update",
        details={"entity_id": entity_id},
    )
    session.add(entry)
    session.flush()
    return entry


def _seed(session) -> tuple[AuditLog, AuditLog, AuditLog]:
    now = datetime.now(timezone.utc)
    old = _mk_entry(session, when=now - timedelta(days=100), entity_id=1)
    mid = _mk_entry(session, when=now - timedelta(days=30), entity_id=2)
    new = _mk_entry(session, when=now - timedelta(days=1), entity_id=3)
    return old, mid, new


# ── Repository ────────────────────────────────────────────────────────────────

def test_count_older_than_with_cutoff_between_rows(session):
    _seed(session)
    cutoff = datetime.now(timezone.utc) - timedelta(days=50)

    count = SqlAuditLogRepository(session).count_older_than(cutoff)

    assert count == 1


def test_count_older_than_cutoff_in_future_counts_all(session):
    _seed(session)
    cutoff = datetime.now(timezone.utc) + timedelta(days=1)

    assert SqlAuditLogRepository(session).count_older_than(cutoff) == 3


def test_count_older_than_cutoff_before_all_counts_zero(session):
    _seed(session)
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)

    assert SqlAuditLogRepository(session).count_older_than(cutoff) == 0


def test_delete_older_than_removes_old_rows_only(session):
    _, mid, new = _seed(session)
    cutoff = datetime.now(timezone.utc) - timedelta(days=50)

    deleted = SqlAuditLogRepository(session).delete_older_than(cutoff)

    assert deleted == 1
    remaining = {e.entity_id for e in session.query(AuditLog).all()}
    assert remaining == {mid.entity_id, new.entity_id}


def test_delete_older_than_noop_when_nothing_old(session):
    _seed(session)
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)

    deleted = SqlAuditLogRepository(session).delete_older_than(cutoff)

    assert deleted == 0
    assert session.query(AuditLog).count() == 3


# ── CLI ───────────────────────────────────────────────────────────────────────

def _run_cli(session, args, *, settings: Settings | None = None):
    """Invoke the maintenance CLI with ``session`` bound to session_scope."""
    @contextmanager
    def _scope():
        yield session

    settings = settings or Settings()
    runner = CliRunner()
    with (
        patch("compendium.cli.commands.maintenance.session_scope", _scope),
        patch("compendium.cli.commands.maintenance.get_settings", return_value=settings),
    ):
        return runner.invoke(maintenance_app, args)


def test_cli_errors_when_no_flag_and_no_setting(session):
    result = _run_cli(session, ["prune-audit-log"])

    assert result.exit_code == 1
    assert "COMPENDIUM_AUDIT_RETENTION_DAYS" in (result.stderr or result.output)


def test_cli_errors_on_zero_days(session):
    result = _run_cli(session, ["prune-audit-log", "--older-than-days", "0"])

    assert result.exit_code == 1
    assert "at least 1 day" in (result.stderr or result.output)


def test_cli_dry_run_reports_without_deleting(session):
    _seed(session)

    result = _run_cli(session, ["prune-audit-log", "--older-than-days", "50", "--dry-run"])

    assert result.exit_code == 0
    assert "Would prune 1" in result.output
    assert session.query(AuditLog).count() == 3


def test_cli_prunes_and_reports_count(session):
    _seed(session)

    result = _run_cli(session, ["prune-audit-log", "--older-than-days", "50"])

    assert result.exit_code == 0
    assert "Pruned 1" in result.output
    assert session.query(AuditLog).count() == 2


def test_cli_uses_setting_when_flag_omitted(session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_AUDIT_RETENTION_DAYS", "50")
    from compendium.services import site_settings as _ss
    _ss.invalidate_cache()
    _seed(session)

    result = _run_cli(session, ["prune-audit-log"])

    assert result.exit_code == 0
    assert "Pruned 1" in result.output
    assert session.query(AuditLog).count() == 2


def test_cli_flag_overrides_setting(session, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_AUDIT_RETENTION_DAYS", "365")
    from compendium.services import site_settings as _ss
    _ss.invalidate_cache()
    _seed(session)

    result = _run_cli(
        session,
        ["prune-audit-log", "--older-than-days", "10"],
    )

    assert result.exit_code == 0
    assert "Pruned 2" in result.output
    assert session.query(AuditLog).count() == 1
