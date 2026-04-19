import typer

from compendium.db.session import session_scope
from compendium.repositories.sql.work_repository import SqlWorkRepository

app = typer.Typer(help="Work (catalog title) commands.")


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
