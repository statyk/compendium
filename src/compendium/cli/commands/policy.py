import getpass

import typer

from compendium.db.session import session_scope
from compendium.domain.errors import DomainError, NotFoundError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.services.audit import AuditService
from compendium.services.policies import PolicyService

app = typer.Typer(help="Loan policy commands.")


def _policy_svc(session) -> PolicyService:
    return PolicyService(
        policy_repo=SqlLoanPolicyRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


@app.command("list")
def list_policies() -> None:
    """List all loan policies."""
    with session_scope() as session:
        policies = _policy_svc(session).list()
        if not policies:
            typer.echo("No loan policies configured.")
            return
        typer.echo("\nLoan policies:")
        for p in policies:
            mt = f"media_type={p.media_type_id}" if p.media_type_id else "default"
            default_flag = "  [DEFAULT]" if p.is_default else ""
            typer.echo(
                f"  #{p.id}  {p.name}  ({mt})  "
                f"{p.loan_period_days}d / {p.max_renewals} renewals{default_flag}"
            )


@app.command("set")
def set_policy(
    policy_id: int = typer.Option(..., "--id", help="Policy ID to update"),
    loan_days: int = typer.Option(None, "--loan-days", help="Loan period in days"),
    max_renewals: int = typer.Option(None, "--max-renewals", help="Maximum renewals"),
) -> None:
    """Update loan period or renewal limit on a policy."""
    if loan_days is None and max_renewals is None:
        typer.echo("Specify at least one of --loan-days or --max-renewals.", err=True)
        raise typer.Exit(1)
    try:
        with session_scope() as session:
            policy = _policy_svc(session).update(
                policy_id, loan_period_days=loan_days, max_renewals=max_renewals
            )
            typer.echo(
                f"Policy #{policy.id} '{policy.name}' updated: "
                f"{policy.loan_period_days}d / {policy.max_renewals} renewals."
            )
    except (DomainError, NotFoundError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
