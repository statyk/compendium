import typer

from compendium.db.session import session_scope
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.services.audit import AuditService

app = typer.Typer(help="Audit log commands.")


@app.command("list")
def list_audit(
    entity: str | None = typer.Option(None, "--entity", help="Entity type (work, item, patron, user, policy)"),
    entity_id: int | None = typer.Option(None, "--id", help="Entity ID"),
    user_id: int | None = typer.Option(None, "--user-id", help="Filter by acting user ID"),
    limit: int = typer.Option(20, "--limit", help="Maximum rows to show"),
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
        if not entries:
            typer.echo("No audit log entries found.")
            return
        header = f"{'ID':>6}  {'Occurred (UTC)':19}  {'Src':3}  {'Actor':20}  {'Entity':7}  {'EID':>6}  {'Action':12}  Details"
        typer.echo(header)
        typer.echo("-" * len(header))
        for e in entries:
            actor = e.actor_label or (f"uid:{e.user_id}" if e.user_id else "—")
            occurred = e.occurred_at.strftime("%Y-%m-%d %H:%M:%S") if e.occurred_at else "—"
            eid = str(e.entity_id) if e.entity_id is not None else "—"
            details_str = str(e.details) if e.details else ""
            typer.echo(
                f"  {e.id:>6}  {occurred:19}  {e.source:3}  {actor:20}  "
                f"{e.entity_type:7}  {eid:>6}  {e.action:12}  {details_str}"
            )
