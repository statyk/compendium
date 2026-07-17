import typer

from compendium.cli.io import error
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
    code_arg: str | None = typer.Argument(None, metavar="CODE"),
    code_opt: str | None = typer.Option(None, "--code", hidden=True),
    classification: str | None = typer.Option(
        None, "--classification", help="Classification scheme: lcc, ddc, or none"
    ),
    name: str | None = typer.Option(None, "--name", help="New display name"),
) -> None:
    """Update a branch's display name and/or classification scheme."""
    from compendium.cli.io import resolve_identifier

    code = resolve_identifier(code_arg, code_opt, label="branch code")

    if classification is None and name is None:
        error("nothing to update — pass --name and/or --classification.")
        raise typer.Exit(1)

    scheme: str | None = None
    if classification is not None:
        scheme = classification.strip().lower()
        if scheme not in _VALID_SCHEMES:
            error(f"invalid scheme '{scheme}'. Must be one of: lcc, ddc, none.")
            raise typer.Exit(1)

    stripped_name: str | None = None
    if name is not None:
        stripped_name = name.strip()
        if not stripped_name or len(stripped_name) > 128:
            error("--name must not be empty and must be 128 characters or fewer.")
            raise typer.Exit(1)

    try:
        with session_scope() as session:
            repo = SqlBranchRepository(session)
            branch = repo.get_by_code(code)
            if branch is None:
                error(f"no branch with code '{code}'.")
                raise typer.Exit(1)
            changes = []
            if stripped_name is not None:
                branch.name = stripped_name
                changes.append(f"name set to '{stripped_name}'")
            if scheme is not None:
                branch.default_classification_scheme = scheme
                label = scheme.upper() if scheme != "none" else "none (manual entry only)"
                changes.append(f"classification scheme set to: {label}")
            repo.update(branch)
            typer.echo(f"Branch '{code}': " + "; ".join(changes))
    except DomainError as exc:
        error(exc)
        raise typer.Exit(1) from exc


from compendium.cli.io import register_alias  # noqa: E402

register_alias(app, "set", branch_set)
