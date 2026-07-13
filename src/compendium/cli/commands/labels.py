"""Label and patron-card PDF generation CLI."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from compendium.cli.io import is_stdio, open_output
from compendium.db.session import session_scope
from compendium.services.labels import (
    DEFAULT_FIELDS,
    ITEM_KIND_TO_FORMAT,
    KIND_DEFAULT_TEMPLATE,
    OPTIONAL_FIELDS,
    PATRON_KIND_TO_FORMAT,
    ItemLabelRow,
    PatronCardRow,
    TEMPLATES,
    compatible_templates,
    generate_item_labels,
    generate_patron_cards,
)
from compendium.services.site_settings import get_site_setting

app = typer.Typer(help="Generate printable PDFs for item labels and patron cards.")


def _parse_date(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError as exc:
        raise typer.BadParameter(f"--since must be YYYY-MM-DD, got '{s}'") from exc


def _collect_items(
    session,
    *,
    barcodes: list[str] | None,
    branch_code: str | None,
    media_type_code: str | None,
    since: str | None,
) -> list[ItemLabelRow]:
    from compendium.domain.models import Branch, Item, MediaType, Work

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
                location=item.location,
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
            category_display=p.category.display_name if p.category else None,
        )
        for p in q.all()
    ]


def _resolve_fields(
    fmt: str,
    show: list[str],
    hide: list[str],
    no_defaults: bool,
) -> frozenset[str]:
    """Build the effective optional-field set from --show / --hide / --no-defaults flags.

    Starts from:
      - Empty set if --no-defaults
      - Admin-configured defaults (falling back to code defaults) otherwise

    Then applies --show (add) and --hide (remove) adjustments.
    Unknown field names produce a warning but are otherwise ignored.
    """
    from compendium.services.settings_registry import get_descriptor
    from compendium.services.site_settings import get_site_setting

    optional = OPTIONAL_FIELDS.get(fmt, frozenset())
    _kind_setting_map = {
        "spine":        "label_spine_default_fields",
        "pocket":       "label_pocket_default_fields",
        "barcode-only": "label_barcode_only_default_fields",
        "full":         "label_patron_full_default_fields",
        "sticker":      "label_patron_sticker_default_fields",
    }

    if no_defaults:
        base: set[str] = set()
    else:
        setting_key = _kind_setting_map.get(fmt)
        if setting_key:
            try:
                stored = get_site_setting(setting_key)
                if isinstance(stored, list):
                    base = set(stored)
                else:
                    base = set(DEFAULT_FIELDS.get(fmt, frozenset()))
            except Exception:
                base = set(DEFAULT_FIELDS.get(fmt, frozenset()))
        else:
            base = set(DEFAULT_FIELDS.get(fmt, frozenset()))

    for f in show:
        if f not in optional:
            typer.echo(f"Warning: '{f}' is not an optional field for this label kind. Ignored.", err=True)
        else:
            base.add(f)
    for f in hide:
        base.discard(f)

    return frozenset(base)


def _run_item_kind(
    kind: str,
    output: str,
    template: str | None,
    show: list[str],
    hide: list[str],
    no_defaults: bool,
    use_isbn_barcode: bool,
    branch: str | None,
    media_type: str | None,
    since: str | None,
    barcodes: str | None,
    start_label: int,
) -> None:
    fmt = ITEM_KIND_TO_FORMAT[kind]
    template_key = template or KIND_DEFAULT_TEMPLATE.get(kind, "avery-5160")
    if template_key not in TEMPLATES:
        typer.echo(f"Error: unknown template '{template_key}'. Run 'labels templates' to list.", err=True)
        raise typer.Exit(1)

    fields = _resolve_fields(fmt, show, hide, no_defaults)
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
        template_key=template_key,
        format=fmt,
        use_isbn_barcode=use_isbn_barcode,
        start_label=start_label,
        fields=fields,
        library_name=get_site_setting("library_name"),
    )
    to_stdout = is_stdio(output)
    with open_output(output, binary=True) as f:
        f.write(pdf)
    where = "stdout" if to_stdout else output
    typer.echo(f"Wrote {len(rows)} label(s) to {where}", err=to_stdout)


def _run_patron_kind(
    kind: str,
    output: str,
    template: str | None,
    show: list[str],
    hide: list[str],
    no_defaults: bool,
    cards: str | None,
    category: str | None,
    active_only: bool,
    start_label: int,
) -> None:
    fmt = PATRON_KIND_TO_FORMAT[kind]
    template_key = template or KIND_DEFAULT_TEMPLATE.get(kind, "avery-5871")
    if template_key not in TEMPLATES:
        typer.echo(f"Error: unknown template '{template_key}'. Run 'labels templates' to list.", err=True)
        raise typer.Exit(1)

    fields = _resolve_fields(fmt, show, hide, no_defaults)
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
            template_key=template_key,
            format=fmt,
            library_name=get_site_setting("library_name"),
            start_label=start_label,
            fields=fields,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    to_stdout = is_stdio(output)
    with open_output(output, binary=True) as f:
        f.write(pdf)
    where = "stdout" if to_stdout else output
    typer.echo(f"Wrote {len(rows)} card(s) to {where}", err=to_stdout)


# ── Shared option definitions ─────────────────────────────────────────────


def _output_opt() -> typer.Option:
    return typer.Option(..., "--output", "-o", help="Output PDF path. Use '-' for stdout.")


def _template_opt() -> typer.Option:
    return typer.Option(None, "--template", help="Sheet template key. Run 'labels templates' to list.")


def _show_opt() -> typer.Option:
    return typer.Option([], "--show", help="Add an optional field (repeatable).")


def _hide_opt() -> typer.Option:
    return typer.Option([], "--hide", help="Remove an optional field (repeatable).")


def _no_defaults_opt() -> typer.Option:
    return typer.Option(False, "--no-defaults", help="Start from empty field set; use --show to add fields.")


def _start_label_opt() -> typer.Option:
    return typer.Option(0, "--start-label", help="Skip the first N labels on the first page.")


def _branch_opt() -> typer.Option:
    return typer.Option(None, "--branch", help="Filter by branch code.")


def _media_type_opt() -> typer.Option:
    return typer.Option(None, "--media-type", help="Filter by media type code.")


def _since_opt() -> typer.Option:
    return typer.Option(None, "--since", help="Items added since YYYY-MM-DD.")


def _barcodes_opt() -> typer.Option:
    return typer.Option(None, "--barcodes", help="Comma-separated barcodes (overrides other filters).")


def _category_opt() -> typer.Option:
    return typer.Option(None, "--category", help="Filter by patron category code.")


def _active_only_opt() -> typer.Option:
    return typer.Option(False, "--active-only", help="Active patrons only.")


def _cards_opt() -> typer.Option:
    return typer.Option(None, "--cards", help="Comma-separated card numbers (overrides other filters).")


# ── Item-label subcommands ────────────────────────────────────────────────


@app.command("spine")
def spine_labels(
    output: str = _output_opt(),
    template: Optional[str] = _template_opt(),
    show: list[str] = _show_opt(),
    hide: list[str] = _hide_opt(),
    no_defaults: bool = _no_defaults_opt(),
    use_isbn_barcode: bool = typer.Option(False, "--use-isbn-barcode"),
    branch: Optional[str] = _branch_opt(),
    media_type: Optional[str] = _media_type_opt(),
    since: Optional[str] = _since_opt(),
    barcodes: Optional[str] = _barcodes_opt(),
    start_label: int = _start_label_opt(),
) -> None:
    """Generate spine labels.

    Optional fields (use --show / --hide): call_number, barcode, location, branch, cutter, year.
    Use --show barcode to add a scannable barcode strip.
    """
    _run_item_kind(
        "spine", output, template, show, hide, no_defaults,
        use_isbn_barcode, branch, media_type, since, barcodes, start_label,
    )


@app.command("pocket")
def pocket_labels(
    output: str = _output_opt(),
    template: Optional[str] = _template_opt(),
    show: list[str] = _show_opt(),
    hide: list[str] = _hide_opt(),
    no_defaults: bool = _no_defaults_opt(),
    use_isbn_barcode: bool = typer.Option(False, "--use-isbn-barcode"),
    branch: Optional[str] = _branch_opt(),
    media_type: Optional[str] = _media_type_opt(),
    since: Optional[str] = _since_opt(),
    barcodes: Optional[str] = _barcodes_opt(),
    start_label: int = _start_label_opt(),
) -> None:
    """Generate pocket labels (title + call number + barcode).

    Optional fields (use --show / --hide): title, author, call_number, barcode, cutter, year, branch, library_name.
    """
    _run_item_kind(
        "pocket", output, template, show, hide, no_defaults,
        use_isbn_barcode, branch, media_type, since, barcodes, start_label,
    )


@app.command("barcode")
def barcode_labels(
    output: str = _output_opt(),
    template: Optional[str] = _template_opt(),
    show: list[str] = _show_opt(),
    hide: list[str] = _hide_opt(),
    no_defaults: bool = _no_defaults_opt(),
    use_isbn_barcode: bool = typer.Option(False, "--use-isbn-barcode"),
    branch: Optional[str] = _branch_opt(),
    media_type: Optional[str] = _media_type_opt(),
    since: Optional[str] = _since_opt(),
    barcodes: Optional[str] = _barcodes_opt(),
    start_label: int = _start_label_opt(),
) -> None:
    """Generate barcode-only stickers.

    Optional fields (use --show / --hide): barcode, title, human_readable.
    """
    _run_item_kind(
        "barcode-only", output, template, show, hide, no_defaults,
        use_isbn_barcode, branch, media_type, since, barcodes, start_label,
    )


# ── Patron-card subcommands ────────────────────────────────────────────────


@app.command("patron-card")
def patron_card(
    output: str = _output_opt(),
    template: Optional[str] = _template_opt(),
    show: list[str] = _show_opt(),
    hide: list[str] = _hide_opt(),
    no_defaults: bool = _no_defaults_opt(),
    cards: Optional[str] = _cards_opt(),
    category: Optional[str] = _category_opt(),
    active_only: bool = _active_only_opt(),
    start_label: int = _start_label_opt(),
) -> None:
    """Generate full patron cards (library name + patron info + barcode).

    Optional fields (use --show / --hide): barcode, card_number, library_name, subtitle, patron_name, expiry, category.
    """
    _run_patron_kind(
        "patron-full", output, template, show, hide, no_defaults,
        cards, category, active_only, start_label,
    )


@app.command("patron-sticker")
def patron_sticker(
    output: str = _output_opt(),
    template: Optional[str] = _template_opt(),
    show: list[str] = _show_opt(),
    hide: list[str] = _hide_opt(),
    no_defaults: bool = _no_defaults_opt(),
    cards: Optional[str] = _cards_opt(),
    category: Optional[str] = _category_opt(),
    active_only: bool = _active_only_opt(),
    start_label: int = _start_label_opt(),
) -> None:
    """Generate patron stickers (barcode only — affix to pre-printed card).

    Optional fields (use --show / --hide): barcode, card_number, patron_name.
    """
    _run_patron_kind(
        "patron-sticker", output, template, show, hide, no_defaults,
        cards, category, active_only, start_label,
    )


@app.command("templates")
def list_templates() -> None:
    """List the available label sheet templates."""
    for t in TEMPLATES.values():
        compat = []
        for kind in list(ITEM_KIND_TO_FORMAT.keys()) + list(PATRON_KIND_TO_FORMAT.keys()):
            if t in compatible_templates(kind):
                compat.append(kind)
        compat_str = ", ".join(compat) if compat else "—"
        typer.echo(f"  {t.key:20s}  {t.display}  ({t.per_sheet}/sheet)  kinds: {compat_str}")
