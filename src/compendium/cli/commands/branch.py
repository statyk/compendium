import typer

from compendium.cli.output import Column, emit_list, format_option
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.branch_repository import SqlBranchRepository

app = typer.Typer(help="Branch commands.")

_VALID_SCHEMES = {"lcc", "ddc", "none"}


@app.command("list")
def branch_list(format: str = format_option()) -> None:
    """List all branches and their classification settings."""
    with session_scope() as session:
        branches = SqlBranchRepository(session).list()
        rows = [
            {
                "code": b.code,
                "name": b.name,
                "is_default": b.is_default,
                "classification_scheme": b.default_classification_scheme,
            }
            for b in branches
        ]
        emit_list(
            rows,
            [
                Column("code", "Code"),
                Column("name", "Name"),
                Column("is_default", "Default", formatter=lambda v: "default" if v else ""),
                Column(
                    "classification_scheme",
                    "Classification",
                    formatter=lambda v: v.upper() if v != "none" else "none",
                ),
            ],
            format,
            empty="No branches configured.",
        )


@app.command("edit")
def branch_set(
    code: str = typer.Option(..., "--code", help="Branch code"),
    classification: str = typer.Option(
        ..., "--classification", help="Classification scheme: lcc, ddc, or none"
    ),
) -> None:
    """Edit a branch's classification scheme."""
    scheme = classification.strip().lower()
    if scheme not in _VALID_SCHEMES:
        typer.echo(
            f"Error: invalid scheme '{scheme}'. Must be one of: lcc, ddc, none.", err=True
        )
        raise typer.Exit(1)

    try:
        with session_scope() as session:
            repo = SqlBranchRepository(session)
            branch = repo.get_by_code(code)
            if branch is None:
                typer.echo(f"Error: no branch with code '{code}'.", err=True)
                raise typer.Exit(1)
            branch.default_classification_scheme = scheme
            repo.update(branch)
            label = scheme.upper() if scheme != "none" else "none (manual entry only)"
            typer.echo(f"Branch '{code}' classification scheme set to: {label}")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


from compendium.cli.io import register_alias  # noqa: E402

register_alias(app, "set", branch_set)
