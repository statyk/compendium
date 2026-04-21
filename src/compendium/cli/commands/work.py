import typer

from compendium.db.session import session_scope
from compendium.domain.errors import DomainError, NotFoundError
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService

app = typer.Typer(help="Work (catalog title) commands.")
creator_app = typer.Typer(help="Manage creators on a specific work.")
app.add_typer(creator_app, name="creator")


def _catalog(session):
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
    )


@app.command("search")
def search_works(
    query: str = typer.Argument(..., help="Search query"),
    field: str = typer.Option(
        "all", "--field", help="Field: all, title, author, publisher, isbn"
    ),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Search the catalog by title, author, publisher, or ISBN."""
    with session_scope() as session:
        works = SqlWorkRepository(session).search(query, field=field, limit=limit)
        if not works:
            typer.echo(f"No results for '{query}'.")
            return
        for w in works:
            creators = ", ".join(wc.creator.display_name for wc in w.creators)
            year = f" ({w.publication_year})" if w.publication_year else ""
            typer.echo(
                f"  [{w.id}] {w.title}" + (f" — {creators}" if creators else "") + year
            )


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


@creator_app.command("set-order")
def creator_set_order(
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
) -> None:
    """Show details for a work, including all copies."""
    with session_scope() as session:
        work = SqlWorkRepository(session).get(work_id)
        if work is None:
            typer.echo(f"No work with id {work_id}.", err=True)
            raise typer.Exit(1)

        creators = ", ".join(
            f"{wc.creator.display_name} ({wc.role})" if wc.role != "author" else wc.creator.display_name
            for wc in work.creators
        )
        typer.echo(f"\n{work.title}" + (f" — {creators}" if creators else ""))
        if work.subtitle:
            typer.echo(f"  Subtitle  : {work.subtitle}")
        if work.publication_year:
            typer.echo(f"  Year      : {work.publication_year}")
        if work.publisher:
            typer.echo(f"  Publisher : {work.publisher}")
        if work.isbn:
            typer.echo(f"  ISBN      : {work.isbn}")
        if work.upc:
            typer.echo(f"  UPC       : {work.upc}")
        if work.media_type:
            typer.echo(f"  Media     : {work.media_type.display_name}")
        if work.classification_code:
            scheme = work.classification_scheme or "?"
            typer.echo(f"  Class     : {scheme} {work.classification_code}")

        if work.items:
            typer.echo(f"\n  Copies ({len(work.items)}):")
            for item in work.items:
                loc = f" @ {item.location}" if item.location else ""
                typer.echo(f"    {item.barcode}  [{item.status}]{loc}")
        else:
            typer.echo("\n  No copies.")
