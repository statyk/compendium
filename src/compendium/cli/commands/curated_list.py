"""Curated list management CLI commands."""
from __future__ import annotations

import getpass
import os

import typer
from sqlalchemy.orm import Session

from compendium.cli.io import error, truncation_notice
from compendium.cli.output import Column, emit_detail, emit_list, format_option
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError
from compendium.domain.models import AppUser
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.curated_list_repository import SqlCuratedListRepository
from compendium.repositories.sql.user_repository import SqlUserRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.curated_lists import CuratedListService

app = typer.Typer(help="Curated list management commands.")


def _svc(session: Session, actor: AppUser | None = None) -> CuratedListService:
    return CuratedListService(
        curated_list_repo=SqlCuratedListRepository(session),
        work_repo=SqlWorkRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor=actor,
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


def _resolve_actor(session: Session) -> AppUser | None:
    username = os.environ.get("COMPENDIUM_ACTOR_USERNAME")
    if not username:
        if SqlUserRepository(session).list(limit=1):
            error(
                "Users exist in this database. Set COMPENDIUM_ACTOR_USERNAME to "
                "an active user whose permissions cover the curated list operations you want to perform."
            )
            raise typer.Exit(1)
        return None
    actor = SqlUserRepository(session).get_by_username(username)
    if actor is None:
        error(f"COMPENDIUM_ACTOR_USERNAME '{username}' not found.")
        raise typer.Exit(1)
    return actor


@app.command("add")
def create_list(
    name: str = typer.Option(..., "--name", "-n", help="List display name"),
    description: str | None = typer.Option(None, "--description", "-d", help="Optional description"),
    public: bool = typer.Option(True, "--public/--private", help="Whether the list is publicly visible"),
    featured: bool = typer.Option(False, "--featured/--not-featured", help="Whether the list is featured"),
    order: int = typer.Option(0, "--order", help="Display order (lower values appear first)"),
) -> None:
    """Create a new curated list."""
    try:
        with session_scope() as session:
            actor = _resolve_actor(session)
            cl = _svc(session, actor).create(
                name=name,
                description=description,
                is_public=public,
                is_featured=featured,
                display_order=order,
            )
        typer.echo(f"Created curated list '{cl.name}' (slug: {cl.slug})")
    except DomainError as e:
        error(e)
        raise typer.Exit(1)


@app.command("list")
def list_lists(
    limit: int = typer.Option(50, "--limit", help="Maximum number of results to return"),
    offset: int = typer.Option(0, "--offset", help="Number of results to skip"),
    public_only: bool = typer.Option(False, "--public-only", help="Show only public lists"),
    featured_only: bool = typer.Option(False, "--featured-only", help="Show only featured lists"),
    format: str = format_option(),
) -> None:
    """List curated lists."""
    with session_scope() as session:
        actor = _resolve_actor(session)
        lists = _svc(session, actor).list(
            limit=limit,
            offset=offset,
            public_only=public_only,
            featured_only=featured_only,
        )
    rows = [
        {
            "slug": cl.slug,
            "name": cl.name,
            "is_public": cl.is_public,
            "is_featured": cl.is_featured,
            "entry_count": len(cl.entries),
        }
        for cl in lists
    ]
    emit_list(
        rows,
        [
            Column("slug", "Slug"),
            Column("name", "Name"),
            Column("is_public", "Visibility", formatter=lambda v: "public" if v else "private"),
            Column("is_featured", "Featured", formatter=lambda v: "featured" if v else ""),
            Column("entry_count", "Works", justify="right"),
        ],
        format,
        empty="No curated lists found.",
    )
    truncation_notice(len(lists), limit)


@app.command("show")
def show_list(
    slug: str = typer.Argument(..., help="List slug"),
    format: str = format_option(),
) -> None:
    """Show details and entries for a curated list."""
    try:
        with session_scope() as session:
            actor = _resolve_actor(session)
            cl = _svc(session, actor).get_by_slug(slug)
            if format == "json":
                obj = {
                    "slug": cl.slug,
                    "name": cl.name,
                    "description": cl.description,
                    "is_public": cl.is_public,
                    "is_featured": cl.is_featured,
                    "display_order": cl.display_order,
                    "entries": [
                        {
                            "display_order": entry.display_order,
                            "work_id": entry.work_id,
                            "title": entry.work.title if entry.work else None,
                            "annotation": entry.annotation,
                        }
                        for entry in sorted(cl.entries, key=lambda e: e.display_order)
                    ],
                }
                emit_detail(obj, format)
                return
            typer.echo(f"Name:          {cl.name}")
            typer.echo(f"Slug:          {cl.slug}")
            typer.echo(f"Description:   {cl.description or '(none)'}")
            typer.echo(f"Visibility:    {'public' if cl.is_public else 'private'}")
            typer.echo(f"Featured:      {'yes' if cl.is_featured else 'no'}")
            typer.echo(f"Display order: {cl.display_order}")
            typer.echo(f"Works:         {len(cl.entries)}")
            if cl.entries:
                typer.echo("")
                for entry in sorted(cl.entries, key=lambda e: e.display_order):
                    title = entry.work.title if entry.work else f"work_id={entry.work_id}"
                    annotation_part = f" — {entry.annotation}" if entry.annotation else ""
                    typer.echo(
                        f"  {entry.display_order}. {title} ({entry.work_id}){annotation_part}"
                    )
    except DomainError as e:
        error(e)
        raise typer.Exit(1)


@app.command("edit")
def edit_list(
    slug: str = typer.Argument(..., help="List slug"),
    name: str | None = typer.Option(None, "--name", "-n", help="New display name"),
    description: str | None = typer.Option(None, "--description", "-d", help="New description"),
    public: bool | None = typer.Option(None, "--public/--private", help="Public or private visibility"),
    featured: bool | None = typer.Option(None, "--featured/--not-featured", help="Featured status"),
    order: int | None = typer.Option(None, "--order", help="New display order"),
    new_slug: str | None = typer.Option(None, "--slug", help="Rename the slug"),
) -> None:
    """Edit a curated list's metadata."""
    try:
        with session_scope() as session:
            actor = _resolve_actor(session)
            svc = _svc(session, actor)
            cl = svc.get_by_slug(slug)

            kwargs: dict = {}
            if name is not None:
                kwargs["name"] = name
            if description is not None:
                kwargs["description"] = description
            if public is not None:
                kwargs["is_public"] = public
            if featured is not None:
                kwargs["is_featured"] = featured
            if order is not None:
                kwargs["display_order"] = order
            if new_slug is not None:
                kwargs["slug"] = new_slug

            if not kwargs:
                error(
                    "provide at least one option to update "
                    "(--name, --description, --public/--private, --featured/--not-featured, --order, --slug)."
                )
                raise typer.Exit(1)

            svc.update(cl.id, **kwargs)
        typer.echo(f"Updated '{slug}'.")
    except DomainError as e:
        error(e)
        raise typer.Exit(1)


@app.command("delete")
def delete_list(
    slug: str = typer.Argument(..., help="List slug"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a curated list and all its entries."""
    if not yes:
        typer.confirm(f"Delete curated list '{slug}'?", abort=True)
    try:
        with session_scope() as session:
            actor = _resolve_actor(session)
            svc = _svc(session, actor)
            cl = svc.get_by_slug(slug)
            svc.delete(cl.id)
        typer.echo(f"Deleted '{slug}'.")
    except DomainError as e:
        error(e)
        raise typer.Exit(1)


@app.command("add-work")
def add_work(
    slug: str = typer.Argument(..., help="List slug"),
    work_id: int | None = typer.Option(None, "--work-id", help="Work ID to add"),
    isbn: str | None = typer.Option(None, "--isbn", help="ISBN to look up and add"),
    annotation: str | None = typer.Option(None, "--annotation", "-a", help="Optional annotation for this entry"),
    # TODO: --barcode lookup (requires item → work_id join via SqlItemRepository)
) -> None:
    """Add a work to a curated list."""
    if work_id is None and isbn is None:
        error("provide at least one of --work-id or --isbn.")
        raise typer.Exit(1)
    try:
        with session_scope() as session:
            actor = _resolve_actor(session)
            svc = _svc(session, actor)

            resolved_work_id: int
            if work_id is not None:
                resolved_work_id = work_id
            else:
                # isbn is not None at this point
                assert isbn is not None
                work = SqlWorkRepository(session).get_by_isbn(isbn)
                if work is None:
                    error(f"no work found with ISBN '{isbn}'.")
                    raise typer.Exit(1)
                resolved_work_id = work.id

            cl = svc.get_by_slug(slug)
            svc.add_work(cl.id, resolved_work_id, annotation=annotation)
        typer.echo(f"Added work {resolved_work_id} to '{slug}'.")
    except DomainError as e:
        error(e)
        raise typer.Exit(1)


@app.command("remove-work")
def remove_work(
    slug: str = typer.Argument(..., help="List slug"),
    work_id: int = typer.Argument(..., help="Work ID to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Remove a work from a curated list."""
    if not yes:
        typer.confirm(f"Remove work {work_id} from '{slug}'?", abort=True)
    try:
        with session_scope() as session:
            actor = _resolve_actor(session)
            svc = _svc(session, actor)
            cl = svc.get_by_slug(slug)
            svc.remove_work(cl.id, work_id)
        typer.echo(f"Removed work {work_id} from '{slug}'.")
    except DomainError as e:
        error(e)
        raise typer.Exit(1)


@app.command("reorder")
def reorder_works(
    slug: str = typer.Argument(..., help="List slug"),
    work_ids: str = typer.Option(..., "--work-ids", help="Comma-separated work IDs in desired order (e.g. '101,102,103')"),
) -> None:
    """Reorder works in a curated list."""
    try:
        ordered_ids = [int(x.strip()) for x in work_ids.split(",") if x.strip()]
    except ValueError:
        error("--work-ids must be a comma-separated list of integers.")
        raise typer.Exit(1)
    if not ordered_ids:
        error("--work-ids cannot be empty.")
        raise typer.Exit(1)
    try:
        with session_scope() as session:
            actor = _resolve_actor(session)
            svc = _svc(session, actor)
            cl = svc.get_by_slug(slug)
            svc.reorder(cl.id, ordered_ids)
        typer.echo(f"Reordered works in '{slug}'.")
    except DomainError as e:
        error(e)
        raise typer.Exit(1)


from compendium.cli.io import register_alias  # noqa: E402

register_alias(app, "create", create_list)
