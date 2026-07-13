import typer

from compendium.cli.output import Column, emit_list, format_option
from compendium.db.session import session_scope
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.services.audit import AuditService

app = typer.Typer(help="Audit log commands.")

_COLUMNS = [
    Column("id", "ID", justify="right"),
    Column(
        "occurred_at",
        "Occurred (UTC)",
        formatter=lambda v: v.strftime("%Y-%m-%d %H:%M:%S") if v else "—",
    ),
    Column("source", "Src"),
    Column("actor", "Actor"),
    Column("entity_type", "Entity"),
    Column("entity_id", "EID", justify="right"),
    Column("action", "Action"),
    # Table rendering only — JSON always gets the raw dict (see cli/output.py).
    Column("details", "Details", formatter=lambda v: str(v) if v else ""),
]


@app.command("list")
def list_audit(
    entity: str | None = typer.Option(None, "--entity", help="Entity type (work, item, patron, user, policy)"),
    entity_id: int | None = typer.Option(None, "--id", help="Entity ID"),
    user_id: int | None = typer.Option(None, "--user-id", help="Filter by acting user ID"),
    limit: int = typer.Option(20, "--limit", help="Maximum rows to show"),
    format: str = format_option(),
) -> None:
    """List audit log entries."""
    with session_scope() as session:
        svc = AuditService(SqlAuditLogRepository(session))
        entries = svc.list(
            entity_type=entity,
            entity_id=entity_id,
            user_id=user_id,
            limit=limit,
        )
        rows = [
            {
                "id": e.id,
                "occurred_at": e.occurred_at,
                "source": e.source,
                "actor": e.actor_label or (f"uid:{e.user_id}" if e.user_id else None),
                "entity_type": e.entity_type,
                "entity_id": e.entity_id,
                "action": e.action,
                "details": e.details,
            }
            for e in entries
        ]
        emit_list(
            rows,
            _COLUMNS,
            format,
            empty="No audit log entries found.",
        )
