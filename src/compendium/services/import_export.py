"""Bulk import/export of Works and Items via CSV and MARC21/MARCXML.

Design notes:
- One summary ``BULK_IMPORT`` audit entry per run; no per-row audits.
- Pre-validates barcode/accession uniqueness at the application layer so that
  a single bad row raises a Python-level ValidationError (session state clean)
  and the next row can proceed. This avoids SAVEPOINTs entirely, which have
  known issues on pysqlite without custom BEGIN events.
- Transaction boundary is owned by the caller (CLI session_scope, API dependency,
  web route, test fixture). The importer flushes but never commits; dry-run
  rolls back via the outer session's rollback.
- Dedup by ISBN/UPC only; no fuzzy title matching.
- CSV is item-centric (one row per physical copy; work metadata repeats).
- MARC export is standards-compliant; item-level fields (barcode, branch,
  loanable) are not included — they would require non-standard local fields.
  MARC import creates one Item per record using the catalog's accession
  generator (barcodes are always auto-minted in the standard 10/14-digit format).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import IO

from pymarc import Field, MARCReader, MARCWriter, Record, Subfield, parse_xml_to_array
from pymarc.marcxml import record_to_xml
from sqlalchemy.orm import Session

from compendium.domain.enums import LoanRestrictionReason
from compendium.domain.errors import BusinessRuleError, ValidationError
from compendium.domain.identifiers import ITEM_TYPE, validate_barcode
from compendium.domain.models import AppUser, Work
from compendium.repositories.base import ItemRepository, WorkRepository
from compendium.services.audit import AuditAction, AuditEntityType, AuditService
from compendium.services._normalization import normalize_title
from compendium.services.catalog import _DEFAULT_CREATOR_ROLE, CatalogService
from compendium.services.metadata import normalize_isbn, normalize_upc

class ImportMode(str, Enum):
    APPEND = "append"
    SKIP_DUPLICATES = "skip-duplicates"
    ERROR_ON_CONFLICT = "error-on-conflict"


@dataclass
class ImportOptions:
    mode: ImportMode = ImportMode.APPEND
    dry_run: bool = False
    default_branch_code: str | None = None
    default_media_type: str | None = None
    # When True, rows with an ISBN/UPC and at least one missing metadata
    # field will be enriched from the appropriate external source
    # (Open Library / MusicBrainz / TMDb) at import time. Disabled by
    # default — bulk imports of clean data shouldn't pay one HTTP per row.
    enrich_from_external: bool = False
    # When False (default): any barcode/accession_number supplied in the CSV
    # row is discarded and a fresh conformant code is minted. When True: the
    # supplied barcode must be a valid 10/14-digit Compendium barcode; rows
    # with non-conformant barcodes are rejected. Enables CSV round-tripping.
    preserve_barcodes: bool = False
    # When False (default): non-UTF-8 bytes are replaced with U+FFFD and the
    # import proceeds with a warning on the report. When True: any decoding
    # error fails the entire import. Affects CSV and LibraryThing TSV reads;
    # MARC has its own encoding rules and ignores this flag.
    strict_encoding: bool = False


def decode_text_bytes(data: bytes, *, strict: bool) -> tuple[str, int]:
    """Decode a text upload to a Python string.

    Returns ``(text, replaced_count)``. When ``strict=False`` and the bytes
    aren't clean UTF-8, falls back to ``errors="replace"`` (each invalid byte
    becomes U+FFFD) and reports how many replacements happened. Real-world
    third-party exports (notably LibraryThing) sometimes emit a handful of
    stray Latin-1/cp1252 bytes inside an otherwise-UTF-8 file; lossy decode
    is the only sensible recovery.
    """
    try:
        return data.decode("utf-8"), 0
    except UnicodeDecodeError:
        if strict:
            raise
    text = data.decode("utf-8", errors="replace")
    return text, text.count("�")


@dataclass
class ImportRowError:
    row_number: int
    identifier: str
    message: str


@dataclass
class ImportReport:
    source: str
    filename: str | None = None
    total_rows: int = 0
    created_works: int = 0
    added_copies: int = 0
    skipped_duplicates: int = 0
    enriched_rows: int = 0  # rows where external lookup contributed metadata
    errors: list[ImportRowError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False


@dataclass
class ExportFilters:
    media_type_code: str | None = None
    branch_code: str | None = None
    since: datetime | None = None


CSV_COLUMNS = [
    "media_type",
    "title",
    "subtitle",
    "authors",
    "publisher",
    "publication_year",
    "isbn",
    "upc",
    "classification_scheme",
    "classification_code",
    "description",
    "language",
    "cover_image_url",
    "barcode",
    "accession_number",
    "branch",
    "call_number",
    "condition",
    "location",
    "is_loanable",
    "loan_restriction_reason",
    "loan_restriction_note",
]
_REQUIRED_CSV_COLUMNS = {"title"}


# MARC leader[6] → compendium media_type code.
_LEADER6_TO_MEDIA_TYPE = {
    "a": "book",
    "t": "book",
    "i": "cd",  # nonmusical sound — treat as cd for now
    "j": "cd",  # musical sound — will disambiguate vinyl via 007
    "g": "dvd",  # projected — will disambiguate via 007
}
_MEDIA_TYPE_TO_LEADER6 = {
    "book": "a",
    "cd": "j",
    "vinyl": "j",
    "dvd": "g",
    "bluray": "g",
    "vhs": "g",
}
# Minimal 007 strings that round-trip through our media_type inference.
# Position 0 = category (s=sound, v=videorecording); other positions follow
# MARC21 Format for Physical Description, padded with 'n' for unknown.
_MEDIA_TYPE_TO_007 = {
    "cd": "sd fungnnmmned",
    "vinyl": "sd bsmennmplnu",
    "dvd": "vd cvaizq",
    "bluray": "vd sbaizq",
    "vhs": "vf baahou",
}


def _infer_media_type(record: Record) -> str | None:
    """Infer a compendium media_type code from a MARC record's leader + 007.
    Returns None if the record's type does not map to any known media_type."""
    leader = str(record.leader)
    rec_type = leader[6] if len(leader) > 6 else ""
    base = _LEADER6_TO_MEDIA_TYPE.get(rec_type)
    if base is None:
        return None
    # Sound: distinguish cd vs vinyl via 007 position 1 (SMD).
    if base in ("cd", "vinyl"):
        for f in record.get_fields("007"):
            v = f.value()
            if len(v) >= 2 and v[0] == "s":
                smd = v[1]
                if smd == "d":
                    return "cd"
                if smd in ("b", "e", "s"):
                    return "vinyl"
        return base
    # Video: distinguish dvd vs bluray vs vhs via 007 position 4.
    if base == "dvd":
        for f in record.get_fields("007"):
            v = f.value()
            if len(v) >= 5 and v[0] == "v":
                fmt = v[4]
                if fmt == "v":
                    return "dvd"
                if fmt in ("s", "b"):
                    return "bluray"
                if fmt == "b":
                    return "vhs"
        return "dvd"
    return base


def _marc_to_meta(record: Record) -> tuple[str | None, dict]:
    """Parse a MARC record into (media_type_code, meta_dict).

    Raises ValidationError if the record is missing required fields (245$a).
    """
    f245_fields = record.get_fields("245")
    if not f245_fields:
        raise ValidationError("MARC record missing 245 (title)")
    f245 = f245_fields[0]
    title_subs = f245.get_subfields("a")
    if not title_subs:
        raise ValidationError("MARC record missing 245$a (title)")
    title = title_subs[0].rstrip(" /:").strip()
    subtitle_subs = f245.get_subfields("b")
    subtitle = subtitle_subs[0].rstrip(" /:").strip() if subtitle_subs else None

    isbn: str | None = None
    for f in record.get_fields("020"):
        subs = f.get_subfields("a")
        if subs:
            isbn = subs[0].split()[0].strip()
            break

    upc: str | None = None
    for f in record.get_fields("024"):
        if getattr(f, "indicator1", " ") == "1":
            subs = f.get_subfields("a")
            if subs:
                upc = subs[0].split()[0].strip()
                break

    publisher: str | None = None
    pub_year: int | None = None
    for tag in ("264", "260"):
        fs = record.get_fields(tag)
        if not fs:
            continue
        f = fs[0]
        b = f.get_subfields("b")
        if b and not publisher:
            publisher = b[0].rstrip(" ,:").strip() or None
        c = f.get_subfields("c")
        if c and pub_year is None:
            m = re.search(r"\d{4}", c[0])
            if m:
                pub_year = int(m.group())
        if publisher or pub_year:
            break

    description: str | None = None
    for f in record.get_fields("520"):
        subs = f.get_subfields("a")
        if subs:
            description = subs[0].strip() or None
            break

    classification_scheme: str | None = None
    classification_code: str | None = None
    lcc = record.get_fields("050")
    if lcc and lcc[0].get_subfields("a"):
        classification_scheme = "lcc"
        classification_code = lcc[0].get_subfields("a")[0].strip()
    else:
        ddc = record.get_fields("082")
        if ddc and ddc[0].get_subfields("a"):
            classification_scheme = "ddc"
            classification_code = ddc[0].get_subfields("a")[0].strip()

    creators: list[tuple[str, str]] = []

    def _add_creator(f: Field, default_role: str) -> None:
        subs_a = f.get_subfields("a")
        if not subs_a:
            return
        name = subs_a[0].rstrip(",. ").strip()
        if not name:
            return
        role_subs = f.get_subfields("e")
        role = role_subs[0].rstrip(",. ").strip() if role_subs else default_role
        creators.append((name, role or default_role))

    for tag in ("100", "110", "111"):
        for f in record.get_fields(tag):
            _add_creator(f, "author")
    for tag in ("700", "710", "711"):
        for f in record.get_fields(tag):
            _add_creator(f, "contributor")

    external_ids: dict = {}
    ctrl = record.get_fields("001")
    if ctrl:
        external_ids["marc_control"] = ctrl[0].value()
    agency = record.get_fields("003")
    if agency:
        external_ids["marc_agency"] = agency[0].value()

    media_type = _infer_media_type(record)

    meta = {
        "title": title,
        "subtitle": subtitle,
        "creators": creators,
        "publisher": publisher,
        "publication_year": pub_year,
        "description": description,
        "language": "en",
        "isbn": isbn,
        "upc": upc,
        "classification_scheme": classification_scheme,
        "classification_code": classification_code,
        "external_ids": external_ids,
        "extra_metadata": {},
    }
    return media_type, meta


def _work_to_marc(work: Work) -> Record:
    record = Record()
    mt_code = work.media_type.code if work.media_type else "book"
    # Adjust leader positions 5 (record status) and 6 (type).
    leader = list(str(record.leader))
    while len(leader) < 24:
        leader.append(" ")
    leader[5] = "n"  # new
    leader[6] = _MEDIA_TYPE_TO_LEADER6.get(mt_code, "a")
    leader[7] = "m"  # monograph
    leader[9] = "a"  # UTF-8
    record.leader = "".join(leader)

    ext = work.external_ids or {}
    if ext.get("marc_control"):
        record.add_field(Field(tag="001", data=str(ext["marc_control"])))
    if ext.get("marc_agency"):
        record.add_field(Field(tag="003", data=str(ext["marc_agency"])))
    mt_007 = _MEDIA_TYPE_TO_007.get(mt_code)
    if mt_007:
        record.add_field(Field(tag="007", data=mt_007))

    if work.isbn:
        record.add_field(
            Field(tag="020", indicators=[" ", " "], subfields=[Subfield("a", work.isbn)])
        )
    if work.upc:
        record.add_field(
            Field(tag="024", indicators=["1", " "], subfields=[Subfield("a", work.upc)])
        )
    if work.classification_scheme == "lcc" and work.classification_code:
        record.add_field(
            Field(
                tag="050",
                indicators=[" ", "4"],
                subfields=[Subfield("a", work.classification_code)],
            )
        )
    elif work.classification_scheme == "ddc" and work.classification_code:
        record.add_field(
            Field(
                tag="082",
                indicators=["0", "4"],
                subfields=[Subfield("a", work.classification_code)],
            )
        )

    creators_ordered = sorted(work.creators, key=lambda c: c.display_order)
    if creators_ordered:
        first = creators_ordered[0]
        record.add_field(
            Field(
                tag="100",
                indicators=["1", " "],
                subfields=[
                    Subfield("a", first.creator.display_name),
                    Subfield("e", first.role),
                ],
            )
        )
        for wc in creators_ordered[1:]:
            record.add_field(
                Field(
                    tag="700",
                    indicators=["1", " "],
                    subfields=[
                        Subfield("a", wc.creator.display_name),
                        Subfield("e", wc.role),
                    ],
                )
            )

    title_value = (work.title or "").rstrip(" /:").strip()
    t_subfields = [Subfield("a", f"{title_value} /")]
    if work.subtitle:
        t_subfields.insert(
            1, Subfield("b", f"{work.subtitle.rstrip(' /:').strip()} :")
        )
    # Non-filing-chars indicator (ind2); default 0.
    record.add_field(Field(tag="245", indicators=["1", "0"], subfields=t_subfields))

    pub_subs: list[Subfield] = []
    if work.publisher:
        pub_subs.append(Subfield("b", work.publisher))
    if work.publication_year:
        pub_subs.append(Subfield("c", str(work.publication_year)))
    if pub_subs:
        record.add_field(Field(tag="264", indicators=[" ", "1"], subfields=pub_subs))

    if work.description:
        record.add_field(
            Field(
                tag="520",
                indicators=[" ", " "],
                subfields=[Subfield("a", work.description)],
            )
        )
    return record


class ImportService:
    def __init__(
        self,
        *,
        session: Session,
        catalog: CatalogService,
        work_repo: WorkRepository,
        item_repo: ItemRepository,
        audit_svc: AuditService | None = None,
        actor: AppUser | None = None,
        actor_label: str | None = None,
        source: str = "system",
    ) -> None:
        self._session = session
        self._catalog = catalog
        self._works = work_repo
        self._items = item_repo
        self._audit = audit_svc
        self._actor = actor
        self._actor_label = actor_label
        self._source = source

    def import_csv(
        self,
        stream: IO[str],
        options: ImportOptions,
        filename: str | None = None,
    ) -> ImportReport:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValidationError("CSV file is empty or missing header row.")

        header = {h.strip() for h in reader.fieldnames if h}
        missing = _REQUIRED_CSV_COLUMNS - header
        if missing:
            raise ValidationError(
                f"CSV missing required columns: {sorted(missing)}"
            )

        report = ImportReport(source="csv", filename=filename, dry_run=options.dry_run)
        seen_barcodes: set[str] = set()
        seen_accessions: set[str] = set()

        for row_num, row in enumerate(reader, start=2):
            report.total_rows += 1
            try:
                _, _, outcome, enriched = self._process_csv_row(
                    row, options, seen_barcodes, seen_accessions
                )
                if enriched:
                    report.enriched_rows += 1
                if outcome == "created_work":
                    report.created_works += 1
                elif outcome == "added_copy":
                    report.added_copies += 1
                elif outcome == "skipped_duplicate":
                    report.skipped_duplicates += 1
                elif outcome == "errored_on_conflict":
                    report.errors.append(
                        ImportRowError(
                            row_number=row_num,
                            identifier=_row_identifier(row),
                            message="Duplicate ISBN/UPC rejected (mode=error-on-conflict)",
                        )
                    )
            except (ValidationError, BusinessRuleError) as exc:
                report.errors.append(
                    ImportRowError(
                        row_number=row_num,
                        identifier=_row_identifier(row),
                        message=str(exc),
                    )
                )
                continue

        return self._finalize(report, options)

    def import_librarything(
        self,
        stream: IO[str],
        options: ImportOptions,
        filename: str | None = None,
    ) -> ImportReport:
        """Import a LibraryThing TSV export.

        Translates LT's 53-column tab-delimited schema into the Compendium
        CSV row contract (lowercase keys + private ``_external_ids`` /
        ``_extra_metadata`` synthetic keys) and delegates per copy to
        ``_process_csv_row``. The MARC importer goes direct to
        ``add_from_import``; this importer reuses the CSV pipeline so it
        inherits dedup, preserve-barcodes, branch defaults, and enrichment.
        """
        reader = csv.DictReader(stream, delimiter="\t")
        if not reader.fieldnames:
            raise ValidationError("LibraryThing TSV is empty or missing header row.")
        header = {h.strip() for h in reader.fieldnames if h}
        if "Title" not in header:
            raise ValidationError(
                "LibraryThing TSV header is missing required column 'Title'. "
                "Got columns: " + ", ".join(sorted(header))
            )

        report = ImportReport(
            source="librarything", filename=filename, dry_run=options.dry_run
        )
        seen_barcodes: set[str] = set()
        seen_accessions: set[str] = set()
        copies_options = replace(
            options, mode=ImportMode.APPEND, enrich_from_external=False
        )

        for row_num, raw in enumerate(reader, start=2):
            report.total_rows += 1
            try:
                compendium_row, copies = _lt_to_compendium(raw)
            except (ValidationError, BusinessRuleError) as exc:
                report.errors.append(
                    ImportRowError(
                        row_number=row_num,
                        identifier=_strip(raw.get("Title")) or "(row)",
                        message=str(exc),
                    )
                )
                continue

            if copies > 1 and options.mode != ImportMode.APPEND:
                report.warnings.append(
                    f"Row {row_num}: forced append mode for copies 2..{copies} "
                    f"because Copies={copies} > 1"
                )
            if copies > 1 and options.enrich_from_external:
                report.warnings.append(
                    f"Row {row_num}: enriched copy 1 only; copies 2..{copies} "
                    "reuse that result without a new external lookup"
                )

            for copy_idx in range(copies):
                if copy_idx == 0:
                    row_for_copy = compendium_row
                    opts_for_copy = options
                else:
                    row_for_copy = dict(compendium_row)
                    row_for_copy["barcode"] = ""
                    opts_for_copy = copies_options
                try:
                    _, _, outcome, enriched = self._process_csv_row(
                        row_for_copy, opts_for_copy, seen_barcodes, seen_accessions
                    )
                    if enriched:
                        report.enriched_rows += 1
                    if outcome == "created_work":
                        report.created_works += 1
                    elif outcome == "added_copy":
                        report.added_copies += 1
                    elif outcome == "skipped_duplicate":
                        report.skipped_duplicates += 1
                    elif outcome == "errored_on_conflict":
                        report.errors.append(
                            ImportRowError(
                                row_number=row_num,
                                identifier=compendium_row.get("title") or "(row)",
                                message=(
                                    "Duplicate ISBN/UPC rejected "
                                    "(mode=error-on-conflict)"
                                ),
                            )
                        )
                except (ValidationError, BusinessRuleError) as exc:
                    report.errors.append(
                        ImportRowError(
                            row_number=row_num,
                            identifier=compendium_row.get("title") or "(row)",
                            message=str(exc),
                        )
                    )
                    break

        return self._finalize(report, options)

    def import_marc(
        self,
        stream: IO[bytes],
        options: ImportOptions,
        filename: str | None = None,
    ) -> ImportReport:
        data = stream.read()
        reader = MARCReader(io.BytesIO(data))
        report = ImportReport(source="marc", filename=filename, dry_run=options.dry_run)
        for idx, record in enumerate(reader, start=1):
            if record is None:
                report.errors.append(
                    ImportRowError(
                        row_number=idx,
                        identifier="(malformed record)",
                        message="Could not parse MARC record",
                    )
                )
                continue
            self._ingest_marc_record(record, idx, options, report)
        return self._finalize(report, options)

    def import_marcxml(
        self,
        stream: IO[bytes],
        options: ImportOptions,
        filename: str | None = None,
    ) -> ImportReport:
        report = ImportReport(
            source="marcxml", filename=filename, dry_run=options.dry_run
        )
        try:
            records = parse_xml_to_array(stream)
        except Exception as exc:
            raise ValidationError(f"Malformed MARCXML: {exc}") from exc
        for idx, record in enumerate(records, start=1):
            self._ingest_marc_record(record, idx, options, report)
        return self._finalize(report, options)

    def _ingest_marc_record(
        self,
        record: Record,
        index: int,
        options: ImportOptions,
        report: ImportReport,
    ) -> None:
        try:
            inferred_mt, meta = _marc_to_meta(record)
        except (ValidationError, BusinessRuleError) as exc:
            report.errors.append(
                ImportRowError(
                    row_number=index,
                    identifier="(record)",
                    message=str(exc),
                )
            )
            return
        mt = inferred_mt or options.default_media_type
        if not mt:
            report.errors.append(
                ImportRowError(
                    row_number=index,
                    identifier=meta.get("title") or "(record)",
                    message=(
                        "MARC leader/007 did not map to a known media_type, and "
                        "no default was given"
                    ),
                )
            )
            return
        if meta.get("isbn"):
            try:
                meta["isbn"] = normalize_isbn(meta["isbn"])
            except ValidationError:
                meta["isbn"] = None
        if meta.get("upc"):
            try:
                meta["upc"] = normalize_upc(meta["upc"])
            except ValidationError:
                meta["upc"] = None

        if options.enrich_from_external and (meta.get("isbn") or meta.get("upc")):
            if self._enrich_meta(meta, mt):
                report.enriched_rows += 1

        try:
            _, _, outcome = self._catalog.add_from_import(
                media_type_code=mt,
                meta=meta,
                conflict_mode=options.mode.value,
                branch_code=options.default_branch_code,
            )
        except (ValidationError, BusinessRuleError) as exc:
            report.errors.append(
                ImportRowError(
                    row_number=index,
                    identifier=meta.get("title") or "(record)",
                    message=str(exc),
                )
            )
            return

        if outcome == "created_work":
            report.created_works += 1
        elif outcome == "added_copy":
            report.added_copies += 1
        elif outcome == "skipped_duplicate":
            report.skipped_duplicates += 1
        elif outcome == "errored_on_conflict":
            report.errors.append(
                ImportRowError(
                    row_number=index,
                    identifier=meta.get("title") or "(record)",
                    message="Duplicate ISBN/UPC rejected (mode=error-on-conflict)",
                )
            )
        report.total_rows += 1

    def _finalize(self, report: ImportReport, options: ImportOptions) -> ImportReport:
        if options.dry_run:
            self._session.rollback()
            return report
        self._session.flush()
        if self._audit is not None:
            self._audit.record(
                actor=self._actor,
                actor_label=self._actor_label,
                source=self._source,
                entity_type=AuditEntityType.WORK,
                entity_id=None,
                action=AuditAction.BULK_IMPORT,
                details={
                    "source": report.source,
                    "filename": report.filename,
                    "total_rows": report.total_rows,
                    "created_works": report.created_works,
                    "added_copies": report.added_copies,
                    "skipped_duplicates": report.skipped_duplicates,
                    "errors": len(report.errors),
                },
            )
            self._session.flush()
        return report

    def _process_csv_row(
        self,
        row: dict,
        options: ImportOptions,
        seen_barcodes: set[str],
        seen_accessions: set[str],
    ):
        # The row dict is the public CSV contract (lowercase column names from
        # _REQUIRED_CSV_COLUMNS). Two private synthetic keys are accepted for
        # programmatic callers (e.g. import_librarything) to inject Work-level
        # metadata that has no CSV column today: ``_external_ids`` (foreign
        # system IDs → Work.external_ids) and ``_extra_metadata`` (publication
        # facts and source-specific blobs → Work.extra_metadata). Both default
        # to {} when absent, so plain CSV imports are unaffected.
        mt = _strip(row.get("media_type")) or options.default_media_type
        if not mt:
            raise ValidationError(
                "media_type is required (row has none and no default was given)"
            )
        mt = mt.lower()

        title = _strip(row.get("title"))
        if not title:
            raise ValidationError("title is required")

        isbn_raw = _strip(row.get("isbn"))
        upc_raw = _strip(row.get("upc"))
        isbn = normalize_isbn(isbn_raw) if isbn_raw else None
        upc = normalize_upc(upc_raw) if upc_raw else None

        pub_year_raw = _strip(row.get("publication_year"))
        pub_year: int | None = None
        if pub_year_raw:
            try:
                pub_year = int(pub_year_raw)
            except ValueError as exc:
                raise ValidationError(
                    f"publication_year '{pub_year_raw}' is not a valid integer"
                ) from exc

        pre_built = row.get("_creators")
        creators: list[tuple[str, str]] = []
        if pre_built:
            # Pre-built pairs from _lt_to_compendium; skip flat authors parser.
            creators = [(str(n).strip(), str(r)) for n, r in pre_built if str(n).strip()]
        else:
            authors_raw = _strip(row.get("authors"))
            if authors_raw:
                default_role = _DEFAULT_CREATOR_ROLE.get(mt, "author")
                for entry in authors_raw.split(";"):
                    entry = entry.strip()
                    if not entry:
                        continue
                    if ":" in entry:
                        name, role = entry.split(":", 1)
                        creators.append((name.strip(), role.strip()))
                    else:
                        creators.append((entry, default_role))

        is_loanable_raw = (_strip(row.get("is_loanable")) or "").lower()
        is_loanable = is_loanable_raw not in {"no", "false", "0"}

        reason = (_strip(row.get("loan_restriction_reason")) or "").lower() or None
        note = _strip(row.get("loan_restriction_note")) or None
        if not is_loanable and reason:
            valid = {r.value for r in LoanRestrictionReason}
            if reason not in valid:
                raise ValidationError(
                    f"Unknown loan_restriction_reason '{reason}' "
                    f"(valid: {sorted(valid)})"
                )
            if reason == LoanRestrictionReason.OTHER.value and not note:
                raise ValidationError(
                    "loan_restriction_note is required when reason is 'other'"
                )
            if reason != LoanRestrictionReason.OTHER.value:
                note = None
        elif is_loanable:
            reason = None
            note = None

        # Barcode/accession handling depends on preserve_barcodes mode.
        barcode = _strip(row.get("barcode"))
        accession = _strip(row.get("accession_number"))
        if options.preserve_barcodes:
            # Validate format; reject non-conformant rows rather than polluting the catalog.
            if barcode and validate_barcode(barcode, expected_type=ITEM_TYPE) is None:
                raise ValidationError(
                    f"barcode '{barcode}' is not a valid Compendium item barcode "
                    f"(expected 10 or 14 digits with Luhn check, type prefix 3)"
                )
            # Pre-validate uniqueness to avoid IntegrityError on flush.
            if barcode:
                if barcode in seen_barcodes:
                    raise ValidationError(
                        f"barcode '{barcode}' appears more than once in this import"
                    )
                if self._items.get_by_barcode(barcode) is not None:
                    raise ValidationError(
                        f"barcode '{barcode}' already exists in the catalog"
                    )
                seen_barcodes.add(barcode)
            if accession:
                if accession in seen_accessions:
                    raise ValidationError(
                        f"accession_number '{accession}' appears more than once in this import"
                    )
                seen_accessions.add(accession)
        else:
            # Default: discard supplied barcode/accession; mint fresh conformant codes.
            barcode = None
            accession = None

        meta = {
            "title": title,
            "subtitle": _strip(row.get("subtitle")),
            "creators": creators,
            "creator_role": _DEFAULT_CREATOR_ROLE.get(mt, "author"),
            "publisher": _strip(row.get("publisher")),
            "publication_year": pub_year,
            "description": _strip(row.get("description")),
            "language": _strip(row.get("language")) or "en",
            "cover_image_url": _strip(row.get("cover_image_url")),
            "isbn": isbn,
            "upc": upc,
            "classification_scheme": _strip(row.get("classification_scheme")),
            "classification_code": _strip(row.get("classification_code")),
            "external_ids": dict(row.get("_external_ids") or {}),
            "extra_metadata": dict(row.get("_extra_metadata") or {}),
        }

        enriched = False
        if options.enrich_from_external and (isbn or upc):
            enriched = self._enrich_meta(meta, mt)

        work, item, outcome = self._catalog.add_from_import(
            media_type_code=mt,
            meta=meta,
            conflict_mode=options.mode.value,
            barcode=barcode,
            accession_number=accession,
            call_number=_strip(row.get("call_number")),
            condition=_strip(row.get("condition")),
            location=_strip(row.get("location")),
            branch_code=_strip(row.get("branch")) or options.default_branch_code,
            is_loanable=is_loanable,
            loan_restriction_reason=reason,
            loan_restriction_note=note,
        )
        return work, item, outcome, enriched

    def _enrich_meta(self, meta: dict, media_type_code: str) -> bool:
        """Fill missing meta fields from the appropriate external source.

        Only triggered when ``ImportOptions.enrich_from_external`` is True
        AND the row has an ISBN or UPC. Network failures are logged and
        swallowed — the row continues to import with whatever the CSV
        provided. Returns True if at least one field was filled.
        """
        from compendium.domain.errors import ExternalLookupError
        from compendium.services.metadata import lookup_metadata

        kind: str | None = None
        value: str | None = None
        if meta.get("isbn"):
            kind, value = "isbn", meta["isbn"]
        elif meta.get("upc"):
            kind, value = "upc", meta["upc"]
        if kind is None:
            return False
        try:
            data = lookup_metadata(media_type_code, kind, value)
        except ExternalLookupError:
            return False
        if not data:
            return False

        filled = False
        # Fill scalar text fields when current value is empty.
        for fname in (
            "subtitle",
            "publisher",
            "publication_year",
            "description",
            "cover_image_url",
        ):
            current = meta.get(fname)
            new = data.get(fname)
            if not new:
                continue
            if current is None or (isinstance(current, str) and not current.strip()):
                meta[fname] = new
                filled = True

        # Authors: only fill if the CSV row provided none.
        if not meta.get("creators") and data.get("authors"):
            default_role = meta.get("creator_role") or "author"
            meta["creators"] = [(name, default_role) for name in data["authors"] if name]
            if meta["creators"]:
                filled = True

        # Merge external_ids without overwriting existing keys.
        external_ids = meta.setdefault("external_ids", {}) or {}
        for k, v in (data.get("external_ids") or {}).items():
            if k not in external_ids and v:
                external_ids[k] = v
                filled = True
        return filled


class ExportService:
    def __init__(self, *, work_repo: WorkRepository) -> None:
        self._works = work_repo

    def export_csv(self, stream: IO[str], filters: ExportFilters) -> int:
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        works = self._works.iter_for_export(
            media_type_code=filters.media_type_code,
            branch_code=filters.branch_code,
            since=filters.since,
        )
        count = 0
        for work in works:
            mt_code = work.media_type.code if work.media_type else ""
            creator_parts = [
                f"{wc.creator.display_name}:{wc.role}"
                for wc in sorted(work.creators, key=lambda x: x.display_order)
            ]
            authors_col = "; ".join(creator_parts)

            items = sorted(work.items, key=lambda i: i.id)
            if not items:
                # Work with no copies — skip in the item-centric export.
                continue
            for item in items:
                if filters.branch_code and item.branch and item.branch.code != filters.branch_code:
                    continue
                writer.writerow(
                    {
                        "media_type": mt_code,
                        "title": work.title or "",
                        "subtitle": work.subtitle or "",
                        "authors": authors_col,
                        "publisher": work.publisher or "",
                        "publication_year": work.publication_year
                        if work.publication_year is not None
                        else "",
                        "isbn": work.isbn or "",
                        "upc": work.upc or "",
                        "classification_scheme": work.classification_scheme or "",
                        "classification_code": work.classification_code or "",
                        "description": work.description or "",
                        "language": work.language or "",
                        "cover_image_url": work.cover_image_url or "",
                        "barcode": item.barcode or "",
                        "accession_number": item.accession_number or "",
                        "branch": item.branch.code if item.branch else "",
                        "call_number": item.call_number or "",
                        "condition": item.condition or "",
                        "location": item.location or "",
                        "is_loanable": "yes" if item.is_loanable else "no",
                        "loan_restriction_reason": item.loan_restriction_reason or "",
                        "loan_restriction_note": item.loan_restriction_note or "",
                    }
                )
                count += 1
        return count

    def export_marc(self, stream: IO[bytes], filters: ExportFilters) -> int:
        writer = MARCWriter(stream)
        works = self._works.iter_for_export(
            media_type_code=filters.media_type_code,
            branch_code=filters.branch_code,
            since=filters.since,
        )
        count = 0
        try:
            for work in works:
                writer.write(_work_to_marc(work))
                count += 1
        finally:
            writer.close(close_fh=False)
        return count

    def export_marcxml(self, stream: IO[bytes], filters: ExportFilters) -> int:
        works = self._works.iter_for_export(
            media_type_code=filters.media_type_code,
            branch_code=filters.branch_code,
            since=filters.since,
        )
        stream.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        stream.write(b'<collection xmlns="http://www.loc.gov/MARC21/slim">\n')
        count = 0
        for work in works:
            xml_bytes = record_to_xml(_work_to_marc(work), namespace=False)
            if isinstance(xml_bytes, str):
                xml_bytes = xml_bytes.encode("utf-8")
            stream.write(xml_bytes)
            stream.write(b"\n")
            count += 1
        stream.write(b"</collection>\n")
        return count


def _strip(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    s = value.strip()
    return s if s else None


def _row_identifier(row: dict) -> str:
    for key in ("isbn", "upc", "barcode", "title"):
        val = _strip(row.get(key))
        if val:
            return val
    return "(unidentified row)"


# LibraryThing's "Media" field uses human labels; map them to compendium codes.
_LT_MEDIA_TO_TYPE: dict[str, str] = {
    "hardcover": "book",
    "paperback": "book",
    "mass market paperback": "book",
    "library binding": "book",
    "trade paperback": "book",
    "softcover": "book",
    "ebook": "book",
    "audiobook": "cd",
    "audiobook (cd)": "cd",
    "audio cd": "cd",
    "music cd": "cd",
    "cd": "cd",
    "vinyl": "vinyl",
    "vinyl record": "vinyl",
    "lp": "vinyl",
    "dvd": "dvd",
    "dvd video": "dvd",
    "blu-ray": "dvd",
    "blu-ray disc": "dvd",
}

# LibraryThing's "Languages" field uses English names; map common ones to ISO
# 639-1 to fit Work.language (String(8)). Anything unmapped ≤ 8 chars passes
# through (caller may already have an ISO code); longer unmapped values are
# dropped so _process_csv_row applies its "en" default.
_LT_LANGUAGE_TO_ISO: dict[str, str] = {
    "english": "en",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "italian": "it",
    "portuguese": "pt",
    "dutch": "nl",
    "polish": "pl",
    "swedish": "sv",
    "norwegian": "no",
    "danish": "da",
    "finnish": "fi",
    "russian": "ru",
    "japanese": "ja",
    "chinese": "zh",
    "korean": "ko",
    "arabic": "ar",
    "hebrew": "he",
    "greek": "el",
    "turkish": "tr",
    "latin": "la",
    "czech": "cs",
    "hungarian": "hu",
    "romanian": "ro",
}

# "Random House (2011), Edition: 1, Hardcover, 448 pages" → ("Random House", "2011")
_LT_PUBLICATION_RE = re.compile(r"^\s*(.+?)\s*\((\d{4})\)")

# LibraryThing wraps ISBNs in square brackets, e.g. "[0940450070]". Empty
# brackets "[]" are emitted for records without an ISBN — those decode to "".
_LT_ISBN_BRACKET_RE = re.compile(r"^\[(.*)\]$")

# Map LibraryThing creator-role tokens (case-insensitive) to CreatorRole values.
# Tokens not in this table fall back to CONTRIBUTOR.
_LT_ROLE_TO_CREATOR_ROLE: dict[str, str] = {
    "author": "author",
    "editor": "editor",
    "translator": "translator",
    "illustrator": "illustrator",
    "narrator": "narrator",
    "introduction": "introduction",
    "foreword": "introduction",
    "preface": "introduction",
    "director": "director",
    "artist": "artist",
    "composer": "composer",
    "performer": "performer",
}


def _normalize_lt_role(token: str | None) -> str:
    """Return a CreatorRole string for a LibraryThing role token."""
    if not token or not token.strip():
        return "author"
    return _LT_ROLE_TO_CREATOR_ROLE.get(token.strip().lower(), "contributor")


def _lt_to_compendium(row: dict) -> tuple[dict, int]:
    """Translate one LibraryThing TSV row into a Compendium CSV row dict.

    Returns ``(row, copies)`` where ``copies`` is the LT ``Copies`` count
    (defaults to 1; values < 1 are clamped). The returned row uses
    Compendium's lowercase column names plus the synthetic ``_external_ids``
    and ``_extra_metadata`` keys consumed by ``_process_csv_row``. Caller
    loops over ``copies`` to mint multiple Items.

    Drops user-attached fields with no Compendium home today (Tags,
    Collections, Rating, Review, Comment, Page Count, etc.) into
    ``extra_metadata["librarything"]`` so a future tags slice can lift them
    out, rather than discarding silently.
    """
    title = normalize_title(_strip(row.get("Title")) or "")
    if not title:
        raise ValidationError("Row is missing required column 'Title'")

    # Build structured (name, role) pairs from the four LT creator columns.
    creators: list[tuple[str, str]] = []
    primary = _strip(row.get("Primary Author"))
    if primary:
        primary_role = _normalize_lt_role(_strip(row.get("Primary Author Role")))
        creators.append((primary, primary_role))
    secondary_names_raw = _strip(row.get("Secondary Author")) or ""
    secondary_roles_raw = _strip(row.get("Secondary Author Roles")) or ""
    sec_names = secondary_names_raw.split("|") if secondary_names_raw else []
    sec_roles = secondary_roles_raw.split("|") if secondary_roles_raw else []
    for i, sec_name in enumerate(sec_names):
        sec_name = sec_name.strip()
        if not sec_name:
            continue
        role_token = sec_roles[i] if i < len(sec_roles) else ""
        creators.append((sec_name, _normalize_lt_role(role_token)))

    # Publication string carries publisher + fallback year; "Date" wins for year.
    publisher: str | None = None
    pub_year_from_publication: str | None = None
    publication_raw = _strip(row.get("Publication"))
    if publication_raw:
        match = _LT_PUBLICATION_RE.match(publication_raw)
        if match:
            publisher = match.group(1).strip() or None
            pub_year_from_publication = match.group(2)
    date_raw = _strip(row.get("Date"))
    # LibraryThing emits "?" or partial dates ("1850-?") for unknown years.
    # Only forward 4-digit numeric strings; otherwise fall back to the
    # year extracted from the Publication string.
    if date_raw and date_raw.isdigit() and len(date_raw) == 4:
        publication_year = date_raw
    else:
        publication_year = pub_year_from_publication

    media_raw = _strip(row.get("Media"))
    media_type: str | None = None
    if media_raw:
        media_type = _LT_MEDIA_TO_TYPE.get(media_raw.lower())

    language_raw = _strip(row.get("Languages"))
    language: str | None = None
    if language_raw:
        first = language_raw.split(",")[0].strip()
        mapped = _LT_LANGUAGE_TO_ISO.get(first.lower())
        if mapped:
            language = mapped
        elif len(first) <= 8:
            language = first

    isbn_raw = _strip(row.get("ISBN"))
    isbn: str | None = None
    if isbn_raw:
        bracket = _LT_ISBN_BRACKET_RE.match(isbn_raw)
        isbn = bracket.group(1).strip() if bracket else isbn_raw
        if not isbn:
            isbn = None

    # LCC wins over DDC (per project's recommended scheme; CLAUDE.md).
    lcc = _strip(row.get("LC Classification"))
    ddc = _strip(row.get("Dewey Decimal"))
    classification_scheme: str | None = None
    classification_code: str | None = None
    if lcc:
        classification_scheme, classification_code = "LCC", lcc
    elif ddc:
        classification_scheme, classification_code = "DDC", ddc

    copies_raw = _strip(row.get("Copies"))
    copies = 1
    if copies_raw:
        try:
            copies = max(1, int(copies_raw))
        except ValueError:
            copies = 1

    # Foreign-system identifiers → Work.external_ids (small, structured).
    external_ids: dict = {}
    lt_ids = {
        k: v
        for k, v in {
            "book_id": _strip(row.get("Book Id")),
            "work_id": _strip(row.get("Work id")),
            "oclc": _strip(row.get("OCLC")),
            "lccn": _strip(row.get("LCCN")),
            "bcid": _strip(row.get("BCID")),
        }.items()
        if v
    }
    if lt_ids:
        external_ids["librarything"] = lt_ids

    # User-attached + publication-fact fields with no Compendium home →
    # Work.extra_metadata. Preserve as a "librarything" sub-dict so a future
    # slice can promote tags/collections/rating to first-class entities.
    lt_extra: dict = {}
    for src_key, dest_key in [
        ("Tags", "tags"),
        ("Collections", "collections"),
    ]:
        raw = _strip(row.get(src_key))
        if raw:
            lt_extra[dest_key] = [p.strip() for p in raw.split(",") if p.strip()]
    for src_key, dest_key in [
        ("Rating", "rating"),
        ("Review", "review"),
        ("Comment", "comment"),
        ("Private Comment", "private_comment"),
        ("Page Count", "page_count"),
        ("Physical Description", "physical_description"),
        ("Original Languages", "original_languages"),
        ("Subjects", "subjects"),
    ]:
        raw = _strip(row.get(src_key))
        if raw:
            lt_extra[dest_key] = raw
    extra_metadata: dict = {}
    if lt_extra:
        extra_metadata["librarything"] = lt_extra

    return (
        {
            "title": title,
            "_creators": creators,
            "authors": "",
            "publisher": publisher or "",
            "publication_year": publication_year or "",
            "media_type": media_type or "",
            "language": language or "",
            "isbn": isbn or "",
            "classification_scheme": classification_scheme or "",
            "classification_code": classification_code or "",
            "call_number": _strip(row.get("Other Call Number")) or "",
            "barcode": _strip(row.get("Barcode")) or "",
            "_external_ids": external_ids,
            "_extra_metadata": extra_metadata,
        },
        copies,
    )
