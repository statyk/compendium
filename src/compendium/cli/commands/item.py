import typer

from compendium.db.session import session_scope
from compendium.domain.errors import DomainError, ExternalLookupError
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService

app = typer.Typer(help="Catalog item commands.")


def _catalog(session):
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
    )


@app.command("add")
def add_item(
    isbn: str | None = typer.Option(None, "--isbn", help="ISBN-10 or ISBN-13 (books)"),
    upc: str | None = typer.Option(None, "--upc", help="UPC/EAN barcode (vinyl, CD)"),
    mbid: str | None = typer.Option(None, "--mbid", help="MusicBrainz release ID (vinyl, CD)"),
    tmdb_id: str | None = typer.Option(None, "--tmdb-id", help="TMDb movie ID (dvd, bluray, vhs) — requires COMPENDIUM_TMDB_API_KEY"),
    media_type: str | None = typer.Option(
        None, "--media-type", help="Media type code: vinyl, cd, dvd, bluray, vhs (required with --upc/--mbid/--tmdb-id)"
    ),
    location: str | None = typer.Option(None, "--location", help="Shelf location note"),
) -> None:
    """Add a new item to the catalog.

    Books:  --isbn <ISBN>
    Music:  --upc <barcode> --media-type vinyl|cd  OR  --mbid <uuid> --media-type vinyl|cd
    Film:   --tmdb-id <id> --media-type dvd|bluray|vhs  (requires COMPENDIUM_TMDB_API_KEY)
    """
    if isbn is not None:
        kind, value, mt_code = "isbn", isbn.strip(), "book"
        typer.echo(f"Looking up ISBN {isbn} on Open Library…")
    elif upc is not None:
        if not media_type:
            typer.echo(
                "Error: --media-type is required with --upc (e.g. vinyl, cd).",
                err=True,
            )
            raise typer.Exit(1)
        kind, value, mt_code = "upc", upc.strip(), media_type.strip()
        typer.echo(f"Looking up UPC {upc} on MusicBrainz…")
    elif mbid is not None:
        mt_code = (media_type or "vinyl").strip()
        kind, value = "mbid", mbid.strip()
        typer.echo(f"Looking up MusicBrainz release {mbid}…")
    elif tmdb_id is not None:
        if not media_type:
            typer.echo(
                "Error: --media-type is required with --tmdb-id (e.g. dvd, bluray, vhs).",
                err=True,
            )
            raise typer.Exit(1)
        kind, value, mt_code = "tmdb_id", tmdb_id.strip(), media_type.strip()
        typer.echo(f"Looking up TMDb ID {tmdb_id}…")
    else:
        typer.echo("Error: provide --isbn, --upc, --mbid, or --tmdb-id.", err=True)
        raise typer.Exit(1)

    try:
        with session_scope() as session:
            work, item = _catalog(session).add_from_lookup(
                mt_code, kind, value, location=location
            )
    except ExternalLookupError as exc:
        typer.echo(f"Lookup failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    creators = ", ".join(
        f"{wc.creator.display_name} ({wc.role})" if wc.role != "author" else wc.creator.display_name
        for wc in work.creators
    )
    typer.echo(f"\nAdded: {work.title}" + (f" — {creators}" if creators else ""))
    if work.publication_year:
        typer.echo(f"  Year      : {work.publication_year}")
    if work.publisher:
        typer.echo(f"  Publisher : {work.publisher}")
    if work.upc:
        typer.echo(f"  UPC       : {work.upc}")
    if work.extra_metadata.get("runtime_minutes"):
        typer.echo(f"  Runtime   : {work.extra_metadata['runtime_minutes']} min")
    if work.extra_metadata.get("genres"):
        typer.echo(f"  Genres    : {', '.join(work.extra_metadata['genres'])}")
    typer.echo(f"  Barcode   : {item.barcode}")
    typer.echo(f"  Accession : {item.accession_number}")
    if item.location:
        typer.echo(f"  Location  : {item.location}")


@app.command("show")
def show_item(
    barcode: str = typer.Argument(..., help="Item barcode"),
) -> None:
    """Show details for an item."""
    try:
        with session_scope() as session:
            repo = SqlItemRepository(session)
            item = repo.get_by_barcode(barcode)
            if item is None:
                typer.echo(f"No item with barcode '{barcode}'.", err=True)
                raise typer.Exit(1)

            work = item.work
            creators = ", ".join(wc.creator.display_name for wc in work.creators)
            typer.echo(f"\n{work.title}" + (f" — {creators}" if creators else ""))
            typer.echo(f"  Barcode   : {item.barcode}")
            typer.echo(f"  Accession : {item.accession_number}")
            typer.echo(f"  Status    : {item.status}")
            typer.echo(f"  Condition : {item.condition or 'not set'}")
            if item.location:
                typer.echo(f"  Location  : {item.location}")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("withdraw")
def withdraw_item(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
) -> None:
    """Withdraw an item from the collection (marks it as withdrawn, not deleted)."""
    try:
        with session_scope() as session:
            item = _catalog(session).withdraw_item(barcode)
            typer.echo(f"\nWithdrawn: {item.work.title}")
            typer.echo(f"  Barcode : {item.barcode}")
            typer.echo(f"  Status  : {item.status}")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("list")
def list_items(
    limit: int = typer.Option(20, "--limit", help="Maximum items to show"),
) -> None:
    """List works in the catalog."""
    with session_scope() as session:
        works = SqlWorkRepository(session).list(limit=limit)
        if not works:
            typer.echo("No items in catalog.")
            return
        for work in works:
            creators = ", ".join(wc.creator.display_name for wc in work.creators)
            copies = len(work.items)
            suffix = f" [{copies} cop{'y' if copies == 1 else 'ies'}]"
            typer.echo(f"  {work.title}" + (f" — {creators}" if creators else "") + suffix)
