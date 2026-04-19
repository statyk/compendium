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
            mt = f"media_type={p.media_type_id}" if p.media_type_id else "general"
            default_flag = "  [DEFAULT]" if p.is_default else ""
            typer.echo(
                f"  #{p.id}  {p.name}  ({mt})  "
                f"{p.loan_period_days}d / {p.max_renewals} renewals{default_flag}"
            )


@app.command("create")
def create_policy(
    name: str = typer.Option(..., "--name", help="Policy name"),
    loan_days: int = typer.Option(..., "--loan-days", help="Loan period in days"),
    max_renewals: int = typer.Option(2, "--max-renewals", help="Maximum renewals"),
    media_type_id: int = typer.Option(None, "--media-type-id", help="Restrict to a specific media type ID"),
    is_default: bool = typer.Option(False, "--default/--no-default", help="Set as the default policy"),
) -> None:
    """Create a new loan policy."""
    try:
        with session_scope() as session:
            policy = _policy_svc(session).create(
                name=name,
                loan_period_days=loan_days,
                max_renewals=max_renewals,
                media_type_id=media_type_id,
                is_default=is_default,
            )
            default_note = " [DEFAULT]" if policy.is_default else ""
            typer.echo(
                f"\nCreated policy #{policy.id} '{policy.name}': "
                f"{policy.loan_period_days}d / {policy.max_renewals} renewals{default_note}."
            )
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("set")
def set_policy(
    policy_id: int = typer.Option(..., "--id", help="Policy ID to update"),
    loan_days: int = typer.Option(None, "--loan-days", help="Loan period in days"),
    max_renewals: int = typer.Option(None, "--max-renewals", help="Maximum renewals"),
    is_default: bool = typer.Option(None, "--default/--no-default", help="Set or clear default flag"),
) -> None:
    """Update loan period, renewal limit, or default flag on a policy."""
    if loan_days is None and max_renewals is None and is_default is None:
        typer.echo("Specify at least one of --loan-days, --max-renewals, or --default/--no-default.", err=True)
        raise typer.Exit(1)
    try:
        with session_scope() as session:
            policy = _policy_svc(session).update(
                policy_id,
                loan_period_days=loan_days,
                max_renewals=max_renewals,
                is_default=is_default,
            )
            default_note = " [DEFAULT]" if policy.is_default else ""
            typer.echo(
                f"Policy #{policy.id} '{policy.name}' updated: "
                f"{policy.loan_period_days}d / {policy.max_renewals} renewals{default_note}."
            )
    except (DomainError, NotFoundError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
