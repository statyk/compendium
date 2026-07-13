import getpass

import typer

from compendium.cli.io import error
from compendium.cli.output import Column, emit_list, format_option
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError, NotFoundError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.patron_category_repository import (
    SqlPatronCategoryRepository,
)
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
def list_policies(format: str = format_option()) -> None:
    """List all loan policies."""
    with session_scope() as session:
        policies = _policy_svc(session).list()
        rows = [
            {
                "id": p.id,
                "name": p.name,
                "media_type_id": p.media_type_id,
                "loan_period_days": p.loan_period_days,
                "max_renewals": p.max_renewals,
                "is_default": p.is_default,
            }
            for p in policies
        ]
        emit_list(
            rows,
            [
                Column("id", "#", justify="right"),
                Column("name", "Name"),
                Column(
                    "media_type_id",
                    "Media",
                    formatter=lambda v: f"media_type={v}" if v else "general",
                ),
                Column("loan_period_days", "Days", justify="right"),
                Column("max_renewals", "Renewals", justify="right"),
                Column("is_default", "Default", formatter=lambda v: "default" if v else ""),
            ],
            format,
            empty="No loan policies configured.",
        )


@app.command("add")
def create_policy(
    name: str = typer.Option(..., "--name", help="Policy name"),
    loan_days: int = typer.Option(..., "--loan-days", help="Loan period in days"),
    max_renewals: int = typer.Option(2, "--max-renewals", help="Maximum renewals"),
    media_type_id: int = typer.Option(None, "--media-type-id", help="Restrict to a specific media type ID"),
    patron_category: str | None = typer.Option(
        None,
        "--patron-category",
        help="Restrict to a patron category code (adult/child/staff/teacher/...)",
    ),
    is_default: bool = typer.Option(False, "--default/--no-default", help="Set as the default policy"),
) -> None:
    """Create a new loan policy."""
    try:
        with session_scope() as session:
            cat_id: int | None = None
            if patron_category:
                cat = SqlPatronCategoryRepository(session).get_by_code(
                    patron_category.lower()
                )
                if cat is None:
                    error(f"No patron category with code '{patron_category}'")
                    raise typer.Exit(1)
                cat_id = cat.id
            policy = _policy_svc(session).create(
                name=name,
                loan_period_days=loan_days,
                max_renewals=max_renewals,
                media_type_id=media_type_id,
                patron_category_id=cat_id,
                is_default=is_default,
            )
            default_note = " [DEFAULT]" if policy.is_default else ""
            typer.echo(
                f"\nCreated policy #{policy.id} '{policy.name}': "
                f"{policy.loan_period_days}d / {policy.max_renewals} renewals{default_note}."
            )
    except DomainError as exc:
        error(exc)
        raise typer.Exit(1) from exc


@app.command("edit")
def set_policy(
    policy_id: int = typer.Option(..., "--id", help="Policy ID to update"),
    loan_days: int = typer.Option(None, "--loan-days", help="Loan period in days"),
    max_renewals: int = typer.Option(None, "--max-renewals", help="Maximum renewals"),
    is_default: bool = typer.Option(None, "--default/--no-default", help="Set or clear default flag"),
    patron_category: str | None = typer.Option(
        None, "--patron-category", help="Patron category code to restrict policy to"
    ),
    clear_patron_category: bool = typer.Option(
        False, "--clear-patron-category", help="Remove the patron-category restriction"
    ),
    overdue_per_day_cents: int = typer.Option(
        None, "--overdue-per-day-cents", help="Overdue fine rate (cents/day)."
    ),
    overdue_cap_cents: int = typer.Option(
        None, "--overdue-cap-cents", help="Max overdue fine per loan (cents)."
    ),
    grace_days: int = typer.Option(
        None, "--grace-days", help="Grace period before overdue fines start."
    ),
    lost_default_cents: int = typer.Option(
        None, "--lost-default-cents", help="Default replacement cost when item declared lost."
    ),
    lost_processing_cents: int = typer.Option(
        None, "--lost-processing-cents", help="Flat processing fee added on lost declarations."
    ),
) -> None:
    """Update loan period, renewal limit, default flag, patron category, or fine settings."""
    if patron_category and clear_patron_category:
        error("--patron-category and --clear-patron-category are mutually exclusive")
        raise typer.Exit(1)
    fine_flags = {
        overdue_per_day_cents,
        overdue_cap_cents,
        grace_days,
        lost_default_cents,
        lost_processing_cents,
    }
    if (
        loan_days is None
        and max_renewals is None
        and is_default is None
        and not patron_category
        and not clear_patron_category
        and all(v is None for v in fine_flags)
    ):
        error(
            "Specify at least one of --loan-days, --max-renewals, --default/--no-default, "
            "--patron-category, --clear-patron-category, --overdue-per-day-cents, "
            "--overdue-cap-cents, --grace-days, --lost-default-cents, --lost-processing-cents."
        )
        raise typer.Exit(1)
    from compendium.services.policies import _MISSING  # sentinel

    try:
        with session_scope() as session:
            cat_arg: object = _MISSING
            if patron_category:
                cat = SqlPatronCategoryRepository(session).get_by_code(
                    patron_category.lower()
                )
                if cat is None:
                    error(f"No patron category with code '{patron_category}'")
                    raise typer.Exit(1)
                cat_arg = cat.id
            elif clear_patron_category:
                cat_arg = None
            policy = _policy_svc(session).update(
                policy_id,
                loan_period_days=loan_days,
                max_renewals=max_renewals,
                is_default=is_default,
                patron_category_id=cat_arg,
                overdue_fine_per_day_cents=overdue_per_day_cents
                if overdue_per_day_cents is not None
                else _MISSING,
                overdue_fine_cap_cents=overdue_cap_cents
                if overdue_cap_cents is not None
                else _MISSING,
                grace_period_days=grace_days,
                lost_item_default_cents=lost_default_cents
                if lost_default_cents is not None
                else _MISSING,
                lost_item_processing_fee_cents=lost_processing_cents
                if lost_processing_cents is not None
                else _MISSING,
            )
            default_note = " [DEFAULT]" if policy.is_default else ""
            typer.echo(
                f"Policy #{policy.id} '{policy.name}' updated: "
                f"{policy.loan_period_days}d / {policy.max_renewals} renewals{default_note}."
            )
    except (DomainError, NotFoundError) as exc:
        error(exc)
        raise typer.Exit(1) from exc


@app.command("delete")
def delete_policy(
    policy_id: int = typer.Option(..., "--id", help="Policy ID to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a loan policy (the default policy cannot be deleted)."""
    if not yes:
        typer.confirm(f"Delete policy #{policy_id}? This cannot be undone.", abort=True)
    try:
        with session_scope() as session:
            svc = _policy_svc(session)
            policy = SqlLoanPolicyRepository(session).get(policy_id)
            name = policy.name if policy else str(policy_id)
            svc.delete(policy_id)
            typer.echo(f"Policy #{policy_id} '{name}' deleted.")
    except (DomainError, NotFoundError) as exc:
        error(exc)
        raise typer.Exit(1) from exc


from compendium.cli.io import register_alias  # noqa: E402

register_alias(app, "create", create_policy)
register_alias(app, "set", set_policy)
