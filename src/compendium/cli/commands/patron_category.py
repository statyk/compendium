"""Patron category CLI commands."""

from __future__ import annotations

import getpass

import typer

from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.patron_category_repository import (
    SqlPatronCategoryRepository,
)
from compendium.services.audit import AuditService
from compendium.services.patron_categories import PatronCategoryService

app = typer.Typer(help="Patron category management commands.")


def _svc(session) -> PatronCategoryService:
    return PatronCategoryService(
        repo=SqlPatronCategoryRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


@app.command("list")
def list_categories() -> None:
    """List patron categories."""
    with session_scope() as session:
        cats = _svc(session).list()
        if not cats:
            typer.echo("No patron categories defined.")
            return
        for c in cats:
            mark = " (default)" if c.is_default else ""
            typer.echo(f"  {c.code:12s}  {c.display_name}{mark}")


@app.command("create")
def create_category(
    code: str = typer.Option(..., "--code", help="Short identifier (e.g. 'adult')"),
    name: str = typer.Option(..., "--name", help="Display name"),
    is_default: bool = typer.Option(False, "--default", help="Mark as the default category"),
) -> None:
    """Create a new patron category."""
    try:
        with session_scope() as session:
            cat = _svc(session).create(code, name, is_default=is_default)
            typer.echo(f"Created category '{cat.code}' ({cat.display_name})")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("update")
def update_category(
    code: str = typer.Option(..., "--code"),
    name: str | None = typer.Option(None, "--name", help="New display name"),
    set_default: bool = typer.Option(False, "--default", help="Mark as the default"),
) -> None:
    """Edit a patron category's display name or default flag."""
    try:
        with session_scope() as session:
            cat = SqlPatronCategoryRepository(session).get_by_code(code.lower())
            if cat is None:
                typer.echo(f"Error: No patron category with code '{code}'", err=True)
                raise typer.Exit(1)
            _svc(session).update(
                cat.id,
                display_name=name,
                is_default=True if set_default else None,
            )
            typer.echo(f"Updated category '{code}'")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("delete")
def delete_category(
    code: str = typer.Option(..., "--code"),
) -> None:
    """Delete a patron category. Refuses if patrons or policies reference it."""
    try:
        with session_scope() as session:
            cat = SqlPatronCategoryRepository(session).get_by_code(code.lower())
            if cat is None:
                typer.echo(f"Error: No patron category with code '{code}'", err=True)
                raise typer.Exit(1)
            _svc(session).delete(cat.id)
            typer.echo(f"Deleted category '{code}'")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
