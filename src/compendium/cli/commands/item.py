import getpass
from datetime import date
from typing import Optional

import typer

from compendium.cli.output import Column, emit_detail, emit_list, format_option
from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.domain.errors import DomainError, ExternalLookupError
from compendium.repositories.sql.audit_log_repository import SqlAuditLogRepository
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.fine_repository import SqlFineRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.item_note_repository import SqlItemNoteRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.loan_policy_repository import SqlLoanPolicyRepository
from compendium.repositories.sql.loan_repository import SqlLoanRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.counters import SqlCounterRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.audit import AuditService
from compendium.services.catalog import CatalogService
from compendium.services.circulation import CirculationService
from compendium.services.fines import FineService
from compendium.services.formatting import format_currency
from compendium.services.item_notes import ItemNoteService
from compendium.services.metadata import (
    musicbrainz_search_title,
    open_library_search_title,
    tmdb_search_title,
)
from compendium.services.site_settings import get_site_setting

app = typer.Typer(help="Catalog item commands.")

note_app = typer.Typer(help="Item note commands.")
app.add_typer(note_app, name="note")

_TITLE_SEARCH_SOURCES: dict[str, tuple[str, str, str]] = {
    # media_type -> (source_label, identifier_kind_for_picks, search_fn_name)
    # Note: book title-search is OL-only (no equivalent GB title-search endpoint).
    # Once an ISBN is picked, the actual metadata lookup uses the configured primary source.
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
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        hold_repo=SqlHoldRepository(session),
        source="cli",
        counter_repo=SqlCounterRepository(session),
        item_note_repo=SqlItemNoteRepository(session),
    )


def _circulation(session) -> CirculationService:
    settings = get_settings()
    audit = AuditService(SqlAuditLogRepository(session))
    fines = FineService(
        fine_repo=SqlFineRepository(session),
        patron_repo=SqlPatronRepository(session),
        loan_repo=SqlLoanRepository(session),
        item_repo=SqlItemRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        settings=settings,
        audit_svc=audit,
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )
    return CirculationService(
        item_repo=SqlItemRepository(session),
        loan_repo=SqlLoanRepository(session),
        patron_repo=SqlPatronRepository(session),
        branch_repo=SqlBranchRepository(session),
        hold_repo=SqlHoldRepository(session),
        policy_repo=SqlLoanPolicyRepository(session),
        hold_pickup_days=get_site_setting("hold_pickup_days"),
        fine_svc=fines,
        audit_svc=audit,
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
        item_note_repo=SqlItemNoteRepository(session),
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
        from compendium.services.metadata import get_book_primary_adapter_name
        _src = get_book_primary_adapter_name()
        typer.echo(f"Looking up ISBN {isbn} on {_src}…")
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


@app.command("add-manual")
def add_manual_item(
    title: str = typer.Option(..., "--title", help="Title (required)"),
    media_type: str = typer.Option(
        "book", "--media-type", help="Media type code: book, vinyl, cd, dvd, bluray, vhs"
    ),
    author: list[str] = typer.Option(
        [], "--author", help="Author/artist/director (repeatable)"
    ),
    publisher: str | None = typer.Option(None, "--publisher"),
    year: int | None = typer.Option(None, "--year", help="Publication year"),
    isbn: str | None = typer.Option(None, "--isbn", help="ISBN, optional"),
    upc: str | None = typer.Option(None, "--upc", help="UPC, optional"),
    description: str | None = typer.Option(None, "--description"),
    location: str | None = typer.Option(None, "--location", help="Shelf location"),
    call_number: str | None = typer.Option(None, "--call-number", help="Call number on the spine label"),
) -> None:
    """Add an item by manually entering its metadata (skips external lookup)."""
    try:
        with session_scope() as session:
            work, item = _catalog(session).add_manual(
                media_type.strip(),
                title,
                authors=list(author),
                publisher=publisher,
                publication_year=year,
                isbn=isbn,
                upc=upc,
                description=description,
                location=location,
            )
            if call_number and call_number.strip():
                item.call_number = call_number.strip()
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    creators = ", ".join(wc.creator.display_name for wc in work.creators)
    typer.echo(f"\nAdded: {work.title}" + (f" — {creators}" if creators else ""))
    if work.publication_year:
        typer.echo(f"  Year      : {work.publication_year}")
    if work.publisher:
        typer.echo(f"  Publisher : {work.publisher}")
    if work.isbn:
        typer.echo(f"  ISBN      : {work.isbn}")
    if work.upc:
        typer.echo(f"  UPC       : {work.upc}")
    typer.echo(f"  Barcode   : {item.barcode}")
    typer.echo(f"  Accession : {item.accession_number}")
    if item.call_number:
        typer.echo(f"  Call #    : {item.call_number}")
    if item.location:
        typer.echo(f"  Location  : {item.location}")


@app.command("edit")
def edit_item(
    barcode_arg: str | None = typer.Argument(None, metavar="BARCODE"),
    barcode_opt: str | None = typer.Option(None, "--barcode", hidden=True),
    location: str | None = typer.Option(
        None, "--location", help="Shelf location. Pass empty string to clear."
    ),
    call_number: str | None = typer.Option(
        None, "--call-number", help="Call number. Pass empty string to clear."
    ),
    condition: str | None = typer.Option(
        None, "--condition", help="Condition note. Pass empty string to clear."
    ),
    notes: str | None = typer.Option(
        None, "--notes", help="Free-form notes. Pass empty string to clear."
    ),
    loanable: bool | None = typer.Option(
        None, "--loanable/--no-loanable", help="Set whether the copy can circulate."
    ),
    loan_note: str | None = typer.Option(
        None, "--loan-note", help="Loan-restriction note (with --no-loanable)."
    ),
) -> None:
    """Edit editable fields on an item (location, call number, condition, notes, loanability).

    Only flags that are passed take effect — omitted flags leave the current
    value alone. Pass an empty string (e.g. ``--location ""``) to clear a
    field.
    """
    from compendium.cli.io import resolve_identifier

    barcode = resolve_identifier(barcode_arg, barcode_opt, label="barcode")

    kwargs: dict[str, str | None] = {}
    if location is not None:
        kwargs["location"] = location
    if call_number is not None:
        kwargs["call_number"] = call_number
    if condition is not None:
        kwargs["condition"] = condition
    if notes is not None:
        kwargs["notes"] = notes
    if not kwargs and loanable is None:
        typer.echo(
            "Nothing to update. Pass one of --location/--call-number/--condition/--notes/--loanable/--no-loanable.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        with session_scope() as session:
            if kwargs:
                item = _catalog(session).update_item(barcode, **kwargs)
            if loanable is not None:
                item = _catalog(session).set_loanable(
                    barcode,
                    is_loanable=loanable,
                    reason=None if loanable else "other",
                    note=loan_note,
                )
            typer.echo(f"\nUpdated: {item.work.title}")
            typer.echo(f"  Barcode   : {item.barcode}")
            typer.echo(f"  Location  : {item.location or 'not set'}")
            typer.echo(f"  Call #    : {item.call_number or 'not set'}")
            typer.echo(f"  Condition : {item.condition or 'not set'}")
            if item.notes:
                typer.echo(f"  Notes     : {item.notes}")
            if loanable is not None:
                typer.echo(f"  Loanable  : {'yes' if item.is_loanable else 'no'}")
                if item.loan_restriction_reason:
                    typer.echo(f"  Reason    : {item.loan_restriction_reason}")
                if item.loan_restriction_note:
                    typer.echo(f"  Note      : {item.loan_restriction_note}")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("show")
def show_item(
    barcode: str = typer.Argument(..., help="Item barcode"),
    format: str = format_option(),
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
            obj = {
                "barcode": item.barcode,
                "accession_number": item.accession_number,
                "status": item.status,
                "is_loanable": item.is_loanable,
                "loan_restriction_reason": item.loan_restriction_reason,
                "loan_restriction_note": item.loan_restriction_note,
                "condition": item.condition,
                "location": item.location,
                "title": work.title,
                "creators": creators,
            }
            if format == "json":
                emit_detail(obj, format)
                return
            title = f"{work.title}" + (f" — {creators}" if creators else "")
            emit_detail(
                {k: v for k, v in obj.items() if v is not None}, format, title=title
            )
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("withdraw")
def withdraw_item(
    barcode_arg: str | None = typer.Argument(None, metavar="BARCODE"),
    barcode_opt: str | None = typer.Option(None, "--barcode", hidden=True),
) -> None:
    """Withdraw an item from the collection (marks it as withdrawn, not deleted)."""
    from compendium.cli.io import resolve_identifier

    barcode = resolve_identifier(barcode_arg, barcode_opt, label="barcode")
    try:
        with session_scope() as session:
            item = _catalog(session).withdraw_item(barcode)
            typer.echo(f"\nWithdrawn: {item.work.title}")
            typer.echo(f"  Barcode : {item.barcode}")
            typer.echo(f"  Status  : {item.status}")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("set-loanable", hidden=True)
def set_loanable_cmd(
    barcode: str = typer.Option(..., "--barcode", help="Item barcode"),
    yes: bool = typer.Option(False, "--yes", help="Mark as loanable (default)"),
    no: bool = typer.Option(False, "--no", help="Mark as non-loanable (requires --reason)"),
    reason: str | None = typer.Option(
        None,
        "--reason",
        help="Required with --no. One of: reference, in_library_use, archive, staff_only, display, other.",
    ),
    note: str | None = typer.Option(
        None, "--note", help="Free-form note (required when --reason other)."
    ),
) -> None:
    """Toggle whether an item can be loaned out.

    Examples:
      compendium item set-loanable --barcode B123 --no --reason reference
      compendium item set-loanable --barcode B123 --no --reason other --note "donor restriction"
      compendium item set-loanable --barcode B123 --yes
    """
    if yes and no:
        typer.echo("Error: pass only one of --yes/--no.", err=True)
        raise typer.Exit(1)
    if not yes and not no:
        typer.echo("Error: pass --yes or --no.", err=True)
        raise typer.Exit(1)
    is_loanable = bool(yes)

    try:
        with session_scope() as session:
            item = _catalog(session).set_loanable(
                barcode,
                is_loanable=is_loanable,
                reason=reason,
                note=note,
            )
            typer.echo(f"\nUpdated: {item.work.title}")
            typer.echo(f"  Barcode  : {item.barcode}")
            typer.echo(f"  Loanable : {'yes' if item.is_loanable else 'no'}")
            if item.loan_restriction_reason:
                typer.echo(f"  Reason   : {item.loan_restriction_reason}")
            if item.loan_restriction_note:
                typer.echo(f"  Note     : {item.loan_restriction_note}")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("declare-lost")
def declare_lost(
    barcode_arg: str | None = typer.Argument(None, metavar="BARCODE"),
    barcode_opt: str | None = typer.Option(None, "--barcode", hidden=True),
    replacement_cost_cents: int | None = typer.Option(
        None,
        "--replacement-cost-cents",
        help="Replacement cost (cents). Defaults to the policy's lost_item_default_cents.",
    ),
    note: str | None = typer.Option(None, "--note"),
) -> None:
    """Declare an item lost. Closes any active loan, cancels pending holds,
    assesses lost + processing fees."""
    from compendium.cli.io import resolve_identifier

    barcode = resolve_identifier(barcode_arg, barcode_opt, label="barcode")
    try:
        with session_scope() as session:
            item = _circulation(session).declare_lost(
                barcode, replacement_cost_cents=replacement_cost_cents, note=note
            )
            typer.echo(
                f"Item {item.barcode} declared lost "
                f"(replacement cost {format_currency(replacement_cost_cents or 0)})."
            )
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("mark-damaged")
def mark_damaged(
    barcode_arg: str | None = typer.Argument(None, metavar="BARCODE"),
    barcode_opt: str | None = typer.Option(None, "--barcode", hidden=True),
    amount_cents: int = typer.Option(..., "--amount-cents"),
    note: str = typer.Option(..., "--note"),
) -> None:
    """Mark an item damaged. Assesses a damaged fee."""
    from compendium.cli.io import resolve_identifier

    barcode = resolve_identifier(barcode_arg, barcode_opt, label="barcode")
    try:
        with session_scope() as session:
            item = _circulation(session).mark_damaged(
                barcode, amount_cents=amount_cents, note=note
            )
            typer.echo(f"Item {item.barcode} marked damaged ({format_currency(amount_cents)}).")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("clear-damage")
def clear_damage(
    barcode_arg: str | None = typer.Argument(None, metavar="BARCODE"),
    barcode_opt: str | None = typer.Option(None, "--barcode", hidden=True),
) -> None:
    """Clear a damaged status and restore the item to AVAILABLE.
    (Any associated damaged-fee is not modified.)"""
    from compendium.cli.io import resolve_identifier

    barcode = resolve_identifier(barcode_arg, barcode_opt, label="barcode")
    try:
        with session_scope() as session:
            item = _circulation(session).clear_damage(barcode)
            typer.echo(f"Item {item.barcode} cleared, now {item.status}.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("clear-lost")
def clear_lost(
    barcode_arg: str | None = typer.Argument(None, metavar="BARCODE"),
    barcode_opt: str | None = typer.Option(None, "--barcode", hidden=True),
) -> None:
    """Clear a lost status and restore the item to AVAILABLE.
    (Any associated lost-fee is not modified.)"""
    from compendium.cli.io import resolve_identifier

    barcode = resolve_identifier(barcode_arg, barcode_opt, label="barcode")
    try:
        with session_scope() as session:
            item = _circulation(session).clear_lost(barcode)
            typer.echo(f"Item {item.barcode} recovered, now {item.status}.")
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


# ---------------------------------------------------------------------------
# Item note sub-commands
# ---------------------------------------------------------------------------


def _note_svc(session) -> ItemNoteService:
    return ItemNoteService(
        item_note_repo=SqlItemNoteRepository(session),
        item_repo=SqlItemRepository(session),
        audit_svc=AuditService(SqlAuditLogRepository(session)),
        actor_label=f"cli:{getpass.getuser()}",
        source="cli",
    )


@note_app.command("add")
def note_add(
    barcode: str = typer.Argument(..., help="Item barcode"),
    note: str = typer.Option(..., "--note", "-n", help="Note text"),
    kind: str = typer.Option(
        "general",
        "--kind",
        "-k",
        help="Note kind (general/condition/repair/provenance/acquisition)",
    ),
    date_str: Optional[str] = typer.Option(
        None, "--date", "-d", help="Event date YYYY-MM-DD"
    ),
) -> None:
    """Add a note to an item."""
    event_date: Optional[date] = None
    if date_str is not None:
        try:
            event_date = date.fromisoformat(date_str)
        except ValueError:
            raise typer.BadParameter(
                f"Invalid date '{date_str}'. Expected YYYY-MM-DD.", param_hint="--date"
            )
    try:
        with session_scope() as session:
            _note_svc(session).add_note(barcode, kind=kind, note=note, event_date=event_date)
        typer.echo(f"Note added to item {barcode}.")
    except DomainError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


_NOTE_COLUMNS = [
    Column("id", "ID", justify="right"),
    Column("event_date", "Date"),
    Column("kind", "Kind"),
    Column("is_system", "System", formatter=lambda v: "system" if v else ""),
    Column("note", "Note", formatter=lambda v: v if len(v) <= 60 else v[:57] + "..."),
]


@note_app.command("list")
def note_list(
    barcode: str = typer.Argument(..., help="Item barcode"),
    format: str = format_option(),
) -> None:
    """List notes for an item."""
    try:
        with session_scope() as session:
            notes = _note_svc(session).list_for_item(barcode)
    except DomainError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    rows = [
        {
            "id": n.id,
            "event_date": n.event_date or n.created_at.date(),
            "kind": n.kind,
            "is_system": n.is_system,
            "note": n.note,
        }
        for n in notes
    ]
    emit_list(rows, _NOTE_COLUMNS, format, empty="No notes found.")


@note_app.command("delete")
def note_delete(
    barcode: str = typer.Argument(..., help="Item barcode"),
    note_id: int = typer.Argument(..., help="Note ID to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a note from an item."""
    if not yes:
        confirmed = typer.confirm(f"Delete note {note_id} from item {barcode}?")
        if not confirmed:
            raise typer.Abort()
    try:
        with session_scope() as session:
            _note_svc(session).delete_note(barcode, note_id)
        typer.echo(f"Note {note_id} deleted.")
    except DomainError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


from compendium.cli.commands.work import list_works_cmd  # noqa: E402
from compendium.cli.io import register_alias  # noqa: E402

register_alias(app, "list", list_works_cmd)
