import typer

from compendium.db.session import session_scope
from compendium.domain.errors import DomainError, ExternalLookupError
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService
from compendium.services.metadata import (
    musicbrainz_search_title,
    open_library_search_title,
    tmdb_search_title,
)

app = typer.Typer(help="Catalog item commands.")

_TITLE_SEARCH_SOURCES: dict[str, tuple[str, str, str]] = {
    # media_type -> (source_label, identifier_kind_for_picks, search_fn_name)
    "book": ("Open Library", "isbn", "open_library"),
    "vinyl": ("MusicBrainz", "mbid", "musicbrainz"),
    "cd": ("MusicBrainz", "mbid", "musicbrainz"),
    "dvd": ("TMDb", "tmdb_id", "tmdb"),
    "bluray": ("TMDb", "tmdb_id", "tmdb"),
    "vhs": ("TMDb", "tmdb_id", "tmdb"),
}


def _title_search(mt_code: str, query: str) -> list[dict]:
    source = _TITLE_SEARCH_SOURCES[mt_code][2]
    if source == "open_library":
        return open_library_search_title(query)
    if source == "musicbrainz":
        return musicbrainz_search_title(query, media_type=mt_code)
    return tmdb_search_title(query)


def _pick_title_candidate(mt_code: str, query: str) -> str | None:
    """Run a title search for the given media type and prompt the user to pick a result.

    Returns the selected candidate's identifier_value (ISBN, MBID, or tmdb_id), or
    None if the user cancels or there are no matches. Exits via typer on
    network/config errors.
    """
    if mt_code not in _TITLE_SEARCH_SOURCES:
        typer.echo(
            f"Error: --title not supported for media type '{mt_code}'.", err=True
        )
        raise typer.Exit(1)
    source_label = _TITLE_SEARCH_SOURCES[mt_code][0]

    typer.echo(f"Searching {source_label} for '{query}'…")
    try:
        candidates = _title_search(mt_code, query)
    except ExternalLookupError as exc:
        typer.echo(f"Lookup failed: {exc}", err=True)
        raise typer.Exit(1) from exc

    if not candidates:
        typer.echo(f"No {source_label} results for '{query}'.")
        return None

    typer.echo("")
    for i, c in enumerate(candidates, start=1):
        year = f" ({c['year']})" if c.get("year") else ""
        tail = f"  [{c['tertiary']}]" if c.get("tertiary") else ""
        typer.echo(f"  {i}. {c['title']}{year}{tail}")
        if c.get("secondary"):
            typer.echo(f"     {c['secondary']}")
    typer.echo("")

    choice = typer.prompt(
        f"Select [1-{len(candidates)}] or 0 to cancel", type=int, default=0
    )
    if choice < 1 or choice > len(candidates):
        typer.echo("Cancelled.")
        return None
    return str(candidates[choice - 1]["identifier_value"])


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
    title: str | None = typer.Option(
        None, "--title", help="Search by title and pick from candidates (requires --media-type)"
    ),
    tmdb_title: str | None = typer.Option(
        None, "--tmdb-title", hidden=True, help="Deprecated alias for --title (film only)."
    ),
    media_type: str | None = typer.Option(
        None, "--media-type", help="Media type code: book, vinyl, cd, dvd, bluray, vhs (required with --upc/--mbid/--tmdb-id/--title)"
    ),
    location: str | None = typer.Option(None, "--location", help="Shelf location note"),
) -> None:
    """Add a new item to the catalog.

    Books:  --isbn <ISBN>  OR  --title "Name" --media-type book
    Music:  --upc <barcode> --media-type vinyl|cd  OR  --mbid <uuid> --media-type vinyl|cd
            --title "Name" --media-type vinyl|cd  (interactive picker via MusicBrainz)
    Film:   --tmdb-id <id> --media-type dvd|bluray|vhs  (requires COMPENDIUM_TMDB_API_KEY)
            --title "Name" --media-type dvd|bluray|vhs  (interactive picker via TMDb)
    """
    if tmdb_title is not None and title is None:
        title = tmdb_title

    if title is not None:
        if not media_type:
            typer.echo(
                "Error: --media-type is required with --title (e.g. book, vinyl, cd, dvd, bluray, vhs).",
                err=True,
            )
            raise typer.Exit(1)
        mt_code = media_type.strip()
        picked = _pick_title_candidate(mt_code, title.strip())
        if picked is None:
            raise typer.Exit(1)
        kind = _TITLE_SEARCH_SOURCES[mt_code][1]
        value = picked
        typer.echo(f"Looking up {kind} {value}…")
    elif isbn is not None:
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
        typer.echo(
            "Error: provide --isbn, --upc, --mbid, --tmdb-id, or --title.", err=True
        )
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
