import getpass

import typer

from compendium.cli.output import Column, emit_detail, emit_list, format_option
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError, NotFoundError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.trash_repository import SqlTrashRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.trash import TrashService

app = typer.Typer(help="Work (catalog title) commands.")
creator_app = typer.Typer(
    help=(
        "Manage creators on a specific work (add/remove/reorder). "
        "To rename a Creator globally, see 'compendium creator rename'."
    )
)
app.add_typer(creator_app, name="creator")
trash_app = typer.Typer(help="Recently deleted works: list, restore, purge.")
app.add_typer(trash_app, name="trash")


def _catalog(session, *, audit: bool = False):
    audit_svc = AuditService(SqlAuditLogRepository(session)) if audit else None
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        audit_svc=audit_svc,
        actor_label=f"cli:{getpass.getuser()}" if audit else None,
        source="cli" if audit else "system",
    )


def _work_row(w) -> dict:
    return {
        "id": w.id,
        "title": w.title,
        "media_type": w.media_type.code if w.media_type else None,
        "creators": ", ".join(wc.creator.display_name for wc in w.creators),
        "publication_year": w.publication_year,
    }


_WORK_COLUMNS = [
    Column("id", "ID", justify="right"),
    Column("title", "Title"),
    Column("media_type", "Media"),
    Column("creators", "Creators"),
    Column("publication_year", "Year", justify="right"),
]


def _print_works(works, fmt: str, empty: str) -> None:
    emit_list([_work_row(w) for w in works], _WORK_COLUMNS, fmt, empty=empty)


_VALID_SORTS = {"title", "author", "recent", "relevance"}


@app.command("search")
def search_works(
    query: str = typer.Argument(..., help="Search query"),
    field: str = typer.Option(
        "all", "--field", help="Field: all, title, author, publisher, isbn"
    ),
    limit: int = typer.Option(20, "--limit"),
    media_type: list[str] = typer.Option(
        [],
        "--media-type",
        help="Filter by media type code (book, dvd, vinyl, …). Repeatable.",
    ),
    decade: int | None = typer.Option(
        None, "--decade", help="Filter by decade start year (e.g. 2010)"
    ),
    available: bool = typer.Option(
        False, "--available/--all", help="Only show works with at least one available copy"
    ),
    include_withdrawn: bool = typer.Option(
        False, "--include-withdrawn/--hide-withdrawn",
        help="Include works whose copies are all withdrawn (hidden by default).",
    ),
    sort: str = typer.Option(
        "title", "--sort", help="Sort order: title, author, recent, relevance."
    ),
    format: str = format_option(),
) -> None:
    """Search the catalog by title, author, publisher, or ISBN."""
    if sort not in _VALID_SORTS:
        raise typer.BadParameter(
            f"Invalid sort '{sort}'. Choose from: {', '.join(sorted(_VALID_SORTS))}",
            param_hint="--sort",
        )
    with session_scope() as session:
        works = SqlWorkRepository(session).search(
            query,
            field=field,
            limit=limit,
            media_type_codes=media_type or None,
            decade=decade,
            available_only=available,
            include_withdrawn_only=include_withdrawn,
            order_by=sort,
        )
        _print_works(works, format, empty=f"No results for '{query}'.")


@app.command("new-arrivals")
def new_arrivals(
    days: int = typer.Option(60, "--days", help="Look back this many days"),
    limit: int = typer.Option(20, "--limit"),
    include_withdrawn: bool = typer.Option(
        False, "--include-withdrawn/--hide-withdrawn",
        help="Include works whose copies are all withdrawn.",
    ),
    format: str = format_option(),
) -> None:
    """List works added to the catalog recently."""
    with session_scope() as session:
        works = SqlWorkRepository(session).list_recent(
            days=days, limit=limit, include_withdrawn_only=include_withdrawn
        )
        _print_works(works, format, empty=f"No works added in the last {days} days.")


@app.command("recently-returned")
def recently_returned(
    days: int = typer.Option(30, "--days", help="Look back this many days"),
    limit: int = typer.Option(20, "--limit"),
    include_withdrawn: bool = typer.Option(
        False, "--include-withdrawn/--hide-withdrawn",
        help="Include works whose copies are all withdrawn.",
    ),
    format: str = format_option(),
) -> None:
    """List works whose most recent return was in the last N days."""
    with session_scope() as session:
        works = SqlWorkRepository(session).list_recently_returned(
            days=days, limit=limit, include_withdrawn_only=include_withdrawn
        )
        _print_works(works, format, empty=f"No works returned in the last {days} days.")


@app.command("edit")
def edit_work(
    work_id: int | None = typer.Option(None, "--work-id", help="Internal Work ID (from `work search`)."),
    isbn: str | None = typer.Option(None, "--isbn", help="Look up the work by ISBN."),
    upc: str | None = typer.Option(None, "--upc", help="Look up the work by UPC."),
    title: str | None = typer.Option(None, "--title", help="New title. Must be non-empty if passed."),
    subtitle: str | None = typer.Option(None, "--subtitle", help="Pass empty string to clear."),
    publisher: str | None = typer.Option(None, "--publisher", help="Pass empty string to clear."),
    year: int | None = typer.Option(None, "--year", help="Publication year. Pass 0 to clear."),
    edition: str | None = typer.Option(None, "--edition", help="Pass empty string to clear."),
    language: str | None = typer.Option(None, "--language", help="2-letter code. Pass empty string to clear."),
    description: str | None = typer.Option(None, "--description", help="Pass empty string to clear."),
    classification_scheme: str | None = typer.Option(None, "--classification-scheme"),
    classification_code: str | None = typer.Option(None, "--classification-code"),
    cover_image_url: str | None = typer.Option(None, "--cover-image-url"),
) -> None:
    """Edit a work's bibliographic fields.

    Identify the work by ``--work-id``, ``--isbn``, or ``--upc``. Only flags
    that are passed take effect; omitted flags leave the current value alone.
    Pass an empty string to clear a text field (``--year 0`` to clear year).
    ISBN, UPC, media type, and creators cannot be edited here.
    """
    supplied = sum(1 for v in (work_id, isbn, upc) if v is not None)
    if supplied != 1:
        typer.echo("Error: provide exactly one of --work-id, --isbn, --upc.", err=True)
        raise typer.Exit(1)

    try:
        with session_scope() as session:
            repo = SqlWorkRepository(session)
            if work_id is not None:
                work = repo.get(work_id)
            elif isbn is not None:
                work = repo.get_by_isbn(isbn.strip())
            else:
                work = repo.get_by_upc(upc.strip())  # type: ignore[union-attr]
            if work is None:
                typer.echo("Error: work not found.", err=True)
                raise typer.Exit(1)

            kwargs: dict = {}
            if title is not None:
                kwargs["title"] = title
            if subtitle is not None:
                kwargs["subtitle"] = subtitle
            if publisher is not None:
                kwargs["publisher"] = publisher
            if year is not None:
                kwargs["publication_year"] = None if year == 0 else year
            if edition is not None:
                kwargs["edition"] = edition
            if language is not None:
                kwargs["language"] = language
            if description is not None:
                kwargs["description"] = description
            if classification_scheme is not None:
                kwargs["classification_scheme"] = classification_scheme
            if classification_code is not None:
                kwargs["classification_code"] = classification_code
            if cover_image_url is not None:
                kwargs["cover_image_url"] = cover_image_url

            if not kwargs:
                typer.echo("Nothing to update. Pass at least one field flag.", err=True)
                raise typer.Exit(1)

            updated = _catalog(session).update_work(work.id, **kwargs)
            typer.echo(f"\nUpdated: {updated.title}")
            if updated.subtitle:
                typer.echo(f"  Subtitle  : {updated.subtitle}")
            if updated.publisher:
                typer.echo(f"  Publisher : {updated.publisher}")
            if updated.publication_year:
                typer.echo(f"  Year      : {updated.publication_year}")
            if updated.classification_code:
                scheme = updated.classification_scheme or "?"
                typer.echo(f"  Class     : {scheme} {updated.classification_code}")
    except NotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _resolve_work(session, work_id, isbn, upc):
    from compendium.repositories.sql.work_repository import SqlWorkRepository

    repo = SqlWorkRepository(session)
    if work_id is not None:
        return repo.get(work_id)
    if isbn is not None:
        return repo.get_by_isbn(isbn.strip())
    if upc is not None:
        return repo.get_by_upc(upc.strip())
    return None


def _require_one_id(work_id, isbn, upc) -> None:
    supplied = sum(1 for v in (work_id, isbn, upc) if v is not None)
    if supplied != 1:
        typer.echo("Error: provide exactly one of --work-id, --isbn, --upc.", err=True)
        raise typer.Exit(1)


@creator_app.command("add")
def creator_add(
    work_id: int | None = typer.Option(None, "--work-id"),
    isbn: str | None = typer.Option(None, "--isbn"),
    upc: str | None = typer.Option(None, "--upc"),
    name: str = typer.Option(..., "--name", help="Creator display name."),
    role: str = typer.Option(..., "--role", help="Creator role (author, director, …)."),
    position: int | None = typer.Option(None, "--position", help="0-based slot to insert at; omit to append."),
) -> None:
    """Add a creator to a work."""
    _require_one_id(work_id, isbn, upc)
    try:
        with session_scope() as session:
            work = _resolve_work(session, work_id, isbn, upc)
            if work is None:
                typer.echo("Error: work not found.", err=True)
                raise typer.Exit(1)
            current = [(wc.creator.display_name, wc.role) for wc in work.creators]
            entry = (name, role)
            if position is None or position >= len(current):
                new_list = current + [entry]
            else:
                pos = max(position, 0)
                new_list = current[:pos] + [entry] + current[pos:]
            updated = _catalog(session).replace_creators(work.id, new_list)
            typer.echo(
                f"Creators for {updated.title}: "
                + ", ".join(f"{wc.creator.display_name} ({wc.role})" for wc in updated.creators)
            )
    except NotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@creator_app.command("remove")
def creator_remove(
    work_id: int | None = typer.Option(None, "--work-id"),
    isbn: str | None = typer.Option(None, "--isbn"),
    upc: str | None = typer.Option(None, "--upc"),
    name: str = typer.Option(..., "--name"),
    role: str = typer.Option(..., "--role"),
) -> None:
    """Remove a creator from a work."""
    _require_one_id(work_id, isbn, upc)
    try:
        with session_scope() as session:
            work = _resolve_work(session, work_id, isbn, upc)
            if work is None:
                typer.echo("Error: work not found.", err=True)
                raise typer.Exit(1)
            new_list = [
                (wc.creator.display_name, wc.role)
                for wc in work.creators
                if not (wc.creator.display_name == name and wc.role == role)
            ]
            if len(new_list) == len(work.creators):
                typer.echo(f"Error: no creator '{name}' with role '{role}' on this work.", err=True)
                raise typer.Exit(1)
            _catalog(session).replace_creators(work.id, new_list)
            typer.echo(f"Removed {name} ({role}).")
    except NotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@creator_app.command("move")
def creator_move(
    work_id: int | None = typer.Option(None, "--work-id"),
    isbn: str | None = typer.Option(None, "--isbn"),
    upc: str | None = typer.Option(None, "--upc"),
    name: str = typer.Option(..., "--name"),
    role: str = typer.Option(..., "--role"),
    position: int = typer.Option(..., "--position", help="0-based target position."),
) -> None:
    """Move a creator to a specific position (0-based)."""
    _require_one_id(work_id, isbn, upc)
    try:
        with session_scope() as session:
            work = _resolve_work(session, work_id, isbn, upc)
            if work is None:
                typer.echo("Error: work not found.", err=True)
                raise typer.Exit(1)
            current = [(wc.creator.display_name, wc.role) for wc in work.creators]
            idx = next(
                (i for i, (n, r) in enumerate(current) if n == name and r == role),
                None,
            )
            if idx is None:
                typer.echo(f"Error: no creator '{name}' with role '{role}' on this work.", err=True)
                raise typer.Exit(1)
            entry = current.pop(idx)
            target = max(0, min(position, len(current)))
            current.insert(target, entry)
            _catalog(session).replace_creators(work.id, current)
            typer.echo("Order updated.")
    except NotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("show")
def show_work(
    work_id: int = typer.Argument(..., help="Work ID"),
    format: str = format_option(),
) -> None:
    """Show details for a work, including all copies."""
    with session_scope() as session:
        work = SqlWorkRepository(session).get(work_id)
        if work is None:
            typer.echo(f"No work with id {work_id}.", err=True)
            raise typer.Exit(1)

        obj = {
            "id": work.id,
            "title": work.title,
            "subtitle": work.subtitle,
            "publication_year": work.publication_year,
            "publisher": work.publisher,
            "isbn": work.isbn,
            "upc": work.upc,
            "media_type": work.media_type.display_name if work.media_type else None,
            "classification_scheme": work.classification_scheme,
            "classification_code": work.classification_code,
            "creators": ", ".join(
                f"{wc.creator.display_name} ({wc.role})" if wc.role != "author"
                else wc.creator.display_name
                for wc in work.creators
            ),
            "items": [
                {"barcode": i.barcode, "status": i.status, "location": i.location}
                for i in work.items
            ],
        }
        if format == "json":
            emit_detail(obj, format)
            return
        emit_detail({k: v for k, v in obj.items() if k != "items" and v is not None},
                    format, title=f"{work.title}")
        typer.echo(f"\n  Copies ({len(work.items)}):" if work.items else "\n  No copies.")
        for item in work.items:
            loc = f" @ {item.location}" if item.location else ""
            typer.echo(f"    {item.barcode}  [{item.status}]{loc}")


@app.command("refresh-metadata")
def refresh_metadata_cmd(
    work_id: int = typer.Option(..., "--work-id", help="Work ID to refresh."),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Commit the changes. Without this flag, the command runs in dry-run mode and prints what would change.",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help=(
            "Force a specific metadata source (books only: 'googlebooks' or 'openlibrary'). "
            "Bypasses the configured primary/fallback chain. "
            "Also implies --bypass-cache."
        ),
    ),
) -> None:
    """Re-fetch metadata for a work from its external source.

    Text fields are filled only when currently empty; the cover URL is
    replaced when upstream offers a new one. The local cover-image cache is
    invalidated on apply (even when the URL is unchanged) so an upstream
    image update at the same URL is picked up on next render.
    """
    try:
        with session_scope() as session:
            report = _catalog(session, audit=apply).refresh_metadata(
                work_id, dry_run=not apply, source=source if source else None,
            )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except NotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    if not report.found:
        typer.echo(f"\nNo refresh applied: {report.error}", err=True)
        raise typer.Exit(1)

    typer.echo(f"\nWork #{report.work_id}")
    typer.echo(f"  Source: {report.source} ({report.lookup_kind}={report.lookup_value})")
    if not report.planned:
        typer.echo("  No fields would change.")
        if apply and report.cover_cache_busted:
            typer.echo("  Cover image cache invalidated (will refresh on next render).")
        return

    typer.echo(f"  Planned changes ({len(report.planned)}):")
    for fname, (old, new) in sorted(report.planned.items()):
        old_str = (old if old not in (None, "") else "(empty)")
        typer.echo(f"    {fname}: {old_str!r} → {new!r}")

    if apply:
        typer.echo(f"\n  Applied {len(report.planned)} change(s).")
        if report.cover_cache_busted:
            typer.echo("  Cover image cache invalidated.")
    else:
        typer.echo("\n  Dry run. Pass --apply to commit.")


def _trash_svc(session) -> TrashService:
    return TrashService(
        trash_repo=SqlTrashRepository(session),
        work_repo=SqlWorkRepository(session),
        hold_repo=SqlHoldRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


@app.command("delete")
def delete_work(
    work_id: int = typer.Argument(..., help="Internal work id (shown by 'work search')."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Move a work and all its copies to the trash (recoverable)."""
    with session_scope() as session:
        work = SqlWorkRepository(session).get(work_id)
        if work is None:
            typer.echo(f"Error: no work with id={work_id}", err=True)
            raise typer.Exit(1)
        n_items = len(work.items)
        if not yes:
            typer.confirm(
                f"Delete '{work.title}' and its {n_items} "
                f"{'copy' if n_items == 1 else 'copies'} (loan history moves to "
                "trash, holds are cancelled)?",
                abort=True,
            )
        try:
            summary = _trash_svc(session).delete_work(work_id)
        except DomainError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Moved to trash: {summary.label} (trash id {summary.trash_id}).")


_TRASH_COLUMNS = [
    Column("trash_id", "Trash ID", justify="right"),
    Column("label", "Label"),
    Column("deleted_at", "Deleted", formatter=lambda v: f"{v:%Y-%m-%d %H:%M} UTC"),
]


@trash_app.command("list")
def trash_list(limit: int = typer.Option(50, "--limit"), format: str = format_option()) -> None:
    """List recently deleted works."""
    with session_scope() as session:
        rows = _trash_svc(session).list_deleted_works(limit=limit)
        row_dicts = [
            {
                "trash_id": r.trash_id,
                "label": r.label,
                "deleted_at": r.deleted_at,
                "original_work_id": r.original_work_id,
                "item_count": r.item_count,
            }
            for r in rows
        ]
        emit_list(row_dicts, _TRASH_COLUMNS, format, empty="Trash is empty.")


@trash_app.command("restore")
def trash_restore(trash_id: int = typer.Argument(...)) -> None:
    """Restore a deleted work from the trash."""
    with session_scope() as session:
        try:
            work = _trash_svc(session).restore_work(trash_id)
        except DomainError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Restored '{work.title}' (work id {work.id}).")


@trash_app.command("purge")
def trash_purge(
    trash_id: int | None = typer.Argument(None, help="Purge one entry permanently."),
    older_than_days: int | None = typer.Option(
        None, "--older-than-days", help="Purge all entries older than N days."
    ),
) -> None:
    """Permanently delete trash entries. Pass an id OR --older-than-days."""
    with session_scope() as session:
        try:
            purged = _trash_svc(session).purge(
                older_than_days=older_than_days,
                trash_id=trash_id,
            )
        except DomainError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Purged {purged} trash entr{'y' if purged == 1 else 'ies'}.")
