"""Label and patron-card PDF generation CLI."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

from compendium.cli.io import is_stdio, open_output
from compendium.db.session import session_scope
from compendium.services.labels import (
    ItemLabelRow,
    PatronCardRow,
    TEMPLATES,
    generate_item_labels,
    generate_patron_cards,
)
from compendium.services.site_settings import get_site_setting

app = typer.Typer(help="Generate printable PDFs for item labels and patron cards.")


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def _collect_items(
    session,
    *,
    barcodes: list[str] | None,
    branch_code: str | None,
    media_type_code: str | None,
    since: str | None,
) -> list[ItemLabelRow]:
    from compendium.domain.models import Branch, Item, MediaType, Work, WorkCreator

    q = session.query(Item).join(Item.work)
    if barcodes:
        q = q.filter(Item.barcode.in_(barcodes))
    if branch_code:
        q = q.join(Item.branch).filter(Branch.code == branch_code)
    if media_type_code:
        q = q.join(Work.media_type).filter(MediaType.code == media_type_code)
    if since:
        q = q.filter(Item.created_at >= _parse_date(since))
    q = q.order_by(Item.accession_number)
    rows: list[ItemLabelRow] = []
    for item in q.all():
        author = ""
        if item.work.creators:
            author = item.work.creators[0].creator.display_name
        rows.append(
            ItemLabelRow(
                barcode=item.barcode,
                title=item.work.title,
                author_display=author,
                call_number=item.call_number,
                publication_year=item.work.publication_year,
                isbn=item.work.isbn,
                branch_code=item.branch.code if item.branch else None,
                location=item.location if item.location else None,
            )
        )
    return rows


def _collect_patrons(
    session,
    *,
    card_numbers: list[str] | None,
    category_code: str | None,
    active_only: bool,
) -> list[PatronCardRow]:
    from compendium.domain.models import Patron, PatronCategory

    q = session.query(Patron)
    if card_numbers:
        q = q.filter(Patron.library_card_number.in_(card_numbers))
    if category_code:
        q = q.join(Patron.category).filter(PatronCategory.code == category_code.lower())
    if active_only:
        q = q.filter(Patron.is_active.is_(True))
    q = q.order_by(Patron.library_card_number)
    return [
        PatronCardRow(
            card_number=p.library_card_number,
            full_name=p.full_name,
            expires_at=p.expires_at,
        )
        for p in q.all()
    ]


@app.command("templates")
def list_templates() -> None:
    """List the available label templates."""
    for t in TEMPLATES.values():
        typer.echo(f"  {t.key:15s}  {t.display}  ({t.per_sheet}/sheet)")


@app.command("items")
def items_labels(
    output: str = typer.Option(
        ..., "--output", "-o",
        help="Output PDF path. Use '-' for stdout.",
    ),
    template: str = typer.Option("avery-5160", "--template"),
    format: str | None = typer.Option(
        None,
        "--format",
        help="spine-text (text-only spine) | spine-barcode (spine + barcode strip) | pocket (info + barcode) | barcode-only (inferred from template if omitted). 'spine' is an alias for 'spine-text'.",
    ),
    use_isbn_barcode: bool = typer.Option(
        False, "--use-isbn-barcode",
        help="Render EAN-13 for items with a valid ISBN; falls back to Code128 otherwise.",
    ),
    branch: str | None = typer.Option(None, "--branch"),
    media_type: str | None = typer.Option(None, "--media-type"),
    since: str | None = typer.Option(None, "--since", help="Items added since YYYY-MM-DD"),
    barcodes: str | None = typer.Option(
        None, "--barcodes", help="Comma-separated list of barcodes (overrides other filters)"
    ),
    start_label: int = typer.Option(0, "--start-label", help="Skip the first N labels on the first page"),
) -> None:
    """Generate an item-label PDF."""
    if template not in TEMPLATES:
        typer.echo(f"Error: unknown template '{template}'. Use 'labels templates' to list.", err=True)
        raise typer.Exit(1)
    if format is not None and format not in ("spine", "spine-text", "spine-barcode", "pocket", "barcode-only"):
        typer.echo(
            "Error: --format must be 'spine', 'spine-text', 'spine-barcode', 'pocket', or 'barcode-only'.",
            err=True,
        )
        raise typer.Exit(1)
    barcode_list = [b.strip() for b in barcodes.split(",")] if barcodes else None
    with session_scope() as session:
        rows = _collect_items(
            session,
            barcodes=barcode_list,
            branch_code=branch,
            media_type_code=media_type,
            since=since,
        )
    if not rows:
        typer.echo("No items matched the filter.", err=True)
        raise typer.Exit(1)
    pdf = generate_item_labels(
        rows,
        template_key=template,
        format=format,
        use_isbn_barcode=use_isbn_barcode,
        start_label=start_label,
    )
    to_stdout = is_stdio(output)
    with open_output(output, binary=True) as f:
        f.write(pdf)
    where = "stdout" if to_stdout else output
    typer.echo(f"Wrote {len(rows)} label(s) to {where}", err=to_stdout)


@app.command("patrons")
def patrons_cards(
    output: str = typer.Option(
        ..., "--output", "-o",
        help="Output PDF path. Use '-' for stdout.",
    ),
    template: str = typer.Option("avery-5871", "--template"),
    format: str = typer.Option("full", "--format", help="full | sticker"),
    cards: str | None = typer.Option(
        None, "--cards", help="Comma-separated list of card numbers (overrides other filters)"
    ),
    category: str | None = typer.Option(None, "--category"),
    active_only: bool = typer.Option(False, "--active-only"),
    start_label: int = typer.Option(0, "--start-label"),
) -> None:
    """Generate a patron-card PDF."""
    if template not in TEMPLATES:
        typer.echo(f"Error: unknown template '{template}'. Use 'labels templates' to list.", err=True)
        raise typer.Exit(1)
    if format not in ("full", "sticker"):
        typer.echo("Error: --format must be 'full' or 'sticker'.", err=True)
        raise typer.Exit(1)
    card_list = [c.strip() for c in cards.split(",")] if cards else None
    with session_scope() as session:
        rows = _collect_patrons(
            session,
            card_numbers=card_list,
            category_code=category,
            active_only=active_only,
        )
    if not rows:
        typer.echo("No patrons matched the filter.", err=True)
        raise typer.Exit(1)
    try:
        pdf = generate_patron_cards(
            rows,
            template_key=template,
            format=format,
            library_name=get_site_setting("library_name"),
            start_label=start_label,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    to_stdout = is_stdio(output)
    with open_output(output, binary=True) as f:
        f.write(pdf)
    where = "stdout" if to_stdout else output
    typer.echo(f"Wrote {len(rows)} card(s) to {where}", err=to_stdout)
