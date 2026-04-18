import typer

from compendium.db.session import session_scope
from compendium.domain.errors import DomainError, ExternalLookupError
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.catalog import CatalogService

app = typer.Typer(help="Catalog item commands.")


def _catalog(session):
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
    )


@app.command("add")
def add_item(
    isbn: str | None = typer.Option(None, "--isbn", help="ISBN-10 or ISBN-13"),
    location: str | None = typer.Option(None, "--location", help="Shelf location note"),
) -> None:
    """Add a new item to the catalog.

    Provide --isbn to look up metadata from Open Library automatically.
    """
    if isbn is None:
        typer.echo("Error: --isbn is required (manual entry not yet supported).", err=True)
        raise typer.Exit(1)

    typer.echo(f"Looking up ISBN {isbn} on Open Library…")
    try:
        with session_scope() as session:
            work, item = _catalog(session).add_from_isbn(isbn, location=location)
    except ExternalLookupError as exc:
        typer.echo(f"Lookup failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    authors = ", ".join(wc.creator.display_name for wc in work.creators)
    typer.echo(f"\nAdded: {work.title}" + (f" — {authors}" if authors else ""))
    if work.publication_year:
        typer.echo(f"  Year      : {work.publication_year}")
    if work.publisher:
        typer.echo(f"  Publisher : {work.publisher}")
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
            authors = ", ".join(wc.creator.display_name for wc in work.creators)
            typer.echo(f"\n{work.title}" + (f" — {authors}" if authors else ""))
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
            authors = ", ".join(wc.creator.display_name for wc in work.creators)
            copies = len(work.items)
            suffix = f" [{copies} cop{'y' if copies == 1 else 'ies'}]"
            typer.echo(f"  {work.title}" + (f" — {authors}" if authors else "") + suffix)
