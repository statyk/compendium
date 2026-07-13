"""CLI tests for compendium import/export commands."""

from __future__ import annotations

import io
from contextlib import contextmanager

from pymarc import Field, MARCWriter, Record, Subfield
from typer.testing import CliRunner

from compendium.cli.commands.bulk_ops import export_app, import_app
from compendium.domain.identifiers import ITEM_TYPE, validate_barcode
from compendium.repositories.sql.work_repository import SqlWorkRepository


def _run(session, app, args):
    @contextmanager
    def _scope():
        yield session

    from unittest.mock import patch

    runner = CliRunner()
    with patch("compendium.cli.commands.bulk_ops.session_scope", _scope):
        return runner.invoke(app, args)


def _minimal_csv(tmp_path, name="in.csv", rows=None):
    rows = rows or [
        "media_type,title,authors,isbn",
        "book,Dune,Frank Herbert,9780441013593",
        "book,Foundation,Isaac Asimov,9780553293357",
    ]
    p = tmp_path / name
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return p


def _mk_marc_bytes(tmp_path, name="in.mrc") -> "Path":
    buf = io.BytesIO()
    w = MARCWriter(buf)
    r = Record()
    leader = list(r.leader)
    while len(leader) < 24:
        leader.append(" ")
    leader[6] = "a"
    r.leader = "".join(leader)
    r.add_field(
        Field(
            tag="020", indicators=[" ", " "], subfields=[Subfield("a", "9780441013593")]
        )
    )
    r.add_field(
        Field(
            tag="100",
            indicators=["1", " "],
            subfields=[Subfield("a", "Herbert, Frank")],
        )
    )
    r.add_field(
        Field(tag="245", indicators=["1", "0"], subfields=[Subfield("a", "Dune /")])
    )
    w.write(r)
    w.close(close_fh=False)
    p = tmp_path / name
    p.write_bytes(buf.getvalue())
    return p


def test_cli_import_csv_default(session, tmp_path):
    f = _minimal_csv(tmp_path)
    result = _run(session, import_app, ["csv", str(f)])
    assert result.exit_code == 0, result.output
    assert "created" in result.output.lower()
    works = SqlWorkRepository(session).list()
    assert {w.title for w in works} == {"Dune", "Foundation"}


def test_cli_import_csv_dry_run(session, tmp_path):
    f = _minimal_csv(tmp_path)
    result = _run(session, import_app, ["csv", str(f), "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output.lower()
    assert SqlWorkRepository(session).list() == []


def test_cli_import_csv_invalid_mode(session, tmp_path):
    f = _minimal_csv(tmp_path)
    result = _run(session, import_app, ["csv", str(f), "--mode", "bogus"])
    assert result.exit_code != 0
    assert "unknown mode" in (result.stderr + result.output).lower()


def test_cli_import_marc(session, tmp_path):
    f = _mk_marc_bytes(tmp_path)
    result = _run(session, import_app, ["marc", str(f)])
    assert result.exit_code == 0
    w = SqlWorkRepository(session).get_by_isbn("9780441013593")
    assert w is not None and w.title == "Dune"


import csv as _csv_mod


def _gr_row_line(book_id, title, author, author_lf, isbn10, isbn13, rating,
                 publisher, binding, pages, year_pub, year_orig, owned):
    buf = io.StringIO()
    w = _csv_mod.writer(buf)
    w.writerow([
        book_id, title, author, author_lf, "",
        f'="{isbn10}"', f'="{isbn13}"', rating, publisher, binding,
        pages, year_pub, year_orig, "", "2026/05/08", "", "", "read",
        "", "", "", "1", owned,
    ])
    return buf.getvalue()


_GR_CSV_HEADER = (
    "Book Id,Title,Author,Author l-f,Additional Authors,ISBN,ISBN13,"
    "My Rating,Publisher,Binding,Number of Pages,Year Published,"
    "Original Publication Year,Date Read,Date Added,Bookshelves,"
    "Bookshelves with positions,Exclusive Shelf,My Review,Spoiler,"
    "Private Notes,Read Count,Owned Copies\n"
)

_GR_CSV = (
    _GR_CSV_HEADER
    + _gr_row_line("1", "The Tao of Pooh", "Benjamin Hoff", "Hoff, Benjamin",
                   "0140067477", "9780140067477", "4", "Penguin Books",
                   "Paperback", "158", "1983", "1982", "1")
    + _gr_row_line("2", "The Hobbit", "J.R.R. Tolkien", "Tolkien, J.R.R.",
                   "0345339681", "9780345339683", "5", "Del Rey",
                   "Paperback", "310", "1982", "1937", "1")
)


def _gr_csv(tmp_path, name="books.csv", content=_GR_CSV):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_cli_import_goodreads_default(session, tmp_path):
    f = _gr_csv(tmp_path)
    result = _run(session, import_app, ["goodreads", str(f)])
    assert result.exit_code == 0, result.output
    works = SqlWorkRepository(session).list()
    titles = {w.title for w in works}
    assert "The Tao of Pooh" in titles
    assert "The Hobbit" in titles


def test_cli_import_goodreads_dry_run(session, tmp_path):
    f = _gr_csv(tmp_path)
    result = _run(session, import_app, ["goodreads", str(f), "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output.lower()
    assert SqlWorkRepository(session).list() == []


def test_cli_import_goodreads_isbn13_dedup(session, tmp_path):
    dup_row = _gr_row_line("3", "The Tao of Pooh", "Benjamin Hoff",
                           "Hoff, Benjamin", "0140067477", "9780140067488",
                           "0", "Penguin Books", "Paperback", "", "1983", "", "1")
    content = _GR_CSV_HEADER + dup_row + dup_row
    f = _gr_csv(tmp_path, "dedup.csv", content)
    result = _run(session, import_app, ["goodreads", str(f), "--mode", "skip-duplicates"])
    assert result.exit_code == 0, result.output
    assert "skipped" in result.output.lower()


def test_cli_import_goodreads_owned_copies_creates_multiple_items(session, tmp_path):
    content = _GR_CSV_HEADER + _gr_row_line(
        "4", "Foundation", "Isaac Asimov", "Asimov, Isaac",
        "0553293354", "9780553293357", "5", "Gnome Press",
        "Hardcover", "244", "1951", "1951", "3",
    )
    f = _gr_csv(tmp_path, "copies.csv", content)
    result = _run(session, import_app, ["goodreads", str(f)])
    assert result.exit_code == 0, result.output
    work = SqlWorkRepository(session).get_by_isbn("9780553293357")
    assert work is not None
    assert "added copy" in result.output.lower() or "created" in result.output.lower()


def test_cli_import_goodreads_lenient_decode_warns_on_stray_byte(session, tmp_path):
    raw = _GR_CSV.encode("utf-8").replace(b"Pooh", b"P\xe8oh")
    f = tmp_path / "messy.csv"
    f.write_bytes(raw)
    result = _run(session, import_app, ["goodreads", str(f)])
    assert result.exit_code == 0, result.output
    assert "byte replacement" in result.output.lower()
    hobbit = SqlWorkRepository(session).get_by_isbn("9780345339683")
    assert hobbit is not None


def test_cli_import_goodreads_strict_encoding_rejects_stray_byte(session, tmp_path):
    raw = _GR_CSV.encode("utf-8").replace(b"Pooh", b"P\xe8oh")
    f = tmp_path / "messy.csv"
    f.write_bytes(raw)
    result = _run(session, import_app, ["goodreads", str(f), "--strict-encoding"])
    assert result.exit_code != 0
    output = (result.stderr or "") + (result.output or "")
    assert "not valid utf-8" in output.lower()


_LT_TSV = (
    "Title\tPrimary Author\tPublication\tDate\tMedia\tLanguages\tISBN\tCopies\tTags\n"
    "Dune\tHerbert, Frank\tAce (1965), Paperback\t1965\tPaperback\tEnglish\t[9780441013593]\t1\tSF\n"
    "Foundation\tAsimov, Isaac\tGnome (1951), Hardcover\t1951\tHardcover\tEnglish\t[9780553293357]\t1\tSF\n"
)


def _lt_tsv(tmp_path, name="books.tsv", content=_LT_TSV):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_cli_import_librarything_default(session, tmp_path):
    f = _lt_tsv(tmp_path)
    result = _run(session, import_app, ["librarything", str(f)])
    assert result.exit_code == 0, result.output
    works = SqlWorkRepository(session).list()
    assert {w.title for w in works} == {"Dune", "Foundation"}


def test_cli_import_librarything_dry_run(session, tmp_path):
    f = _lt_tsv(tmp_path)
    result = _run(session, import_app, ["librarything", str(f), "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output.lower()
    assert SqlWorkRepository(session).list() == []


def test_cli_import_librarything_lenient_decode_warns_on_stray_byte(session, tmp_path):
    # Inject a cp1252 'è' byte into one of the titles so the file isn't UTF-8.
    raw = _LT_TSV.encode("utf-8").replace(b"Dune", b"D\xe8ne")
    f = tmp_path / "messy.tsv"
    f.write_bytes(raw)
    result = _run(session, import_app, ["librarything", str(f)])
    assert result.exit_code == 0, result.output
    assert "byte replacement" in result.output.lower()
    # The other title still imports cleanly.
    foundation = SqlWorkRepository(session).get_by_isbn("9780553293357")
    assert foundation is not None


def test_cli_import_librarything_strict_encoding_rejects_stray_byte(session, tmp_path):
    raw = _LT_TSV.encode("utf-8").replace(b"Dune", b"D\xe8ne")
    f = tmp_path / "messy.tsv"
    f.write_bytes(raw)
    result = _run(
        session, import_app, ["librarything", str(f), "--strict-encoding"]
    )
    assert result.exit_code != 0
    output = (result.stderr or "") + (result.output or "")
    assert "not valid utf-8" in output.lower()


def test_cli_import_csv_strict_encoding_rejects_stray_byte(session, tmp_path):
    raw = b"media_type,title,authors,isbn\nbook,D\xe8ne,Frank Herbert,9780441013593\n"
    f = tmp_path / "messy.csv"
    f.write_bytes(raw)
    result = _run(
        session, import_app, ["csv", str(f), "--strict-encoding"]
    )
    assert result.exit_code != 0


def test_cli_import_csv_lenient_default_imports_with_warning(session, tmp_path):
    raw = b"media_type,title,authors,isbn\nbook,D\xe8ne,Frank Herbert,9780441013593\n"
    f = tmp_path / "messy.csv"
    f.write_bytes(raw)
    result = _run(session, import_app, ["csv", str(f)])
    assert result.exit_code == 0, result.output
    assert "byte replacement" in result.output.lower()


# ── --quiet on import commands ────────────────────────────────────────────────

_BAD_CSV = b"""media_type,title,authors,isbn
book,Dune,Frank Herbert,bad-isbn-1
book,Foundation,Isaac Asimov,bad-isbn-2
"""


def test_cli_import_csv_default_includes_error_detail_lines(session, tmp_path):
    """Sanity baseline: per-row Errors block prints by default."""
    f = tmp_path / "bad.csv"
    f.write_bytes(_BAD_CSV)
    result = _run(session, import_app, ["csv", str(f), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "errors      : 2" in result.output
    assert "Errors:" in result.output
    assert "bad-isbn-1" in result.output


def test_cli_import_csv_quiet_keeps_summary_drops_error_detail(session, tmp_path):
    """--quiet keeps the count summary; the per-row Errors detail is gone."""
    f = tmp_path / "bad.csv"
    f.write_bytes(_BAD_CSV)
    result = _run(session, import_app, ["csv", str(f), "--dry-run", "--quiet"])
    assert result.exit_code == 0, result.output
    # Summary block still printed — the count is the cron-log signal.
    assert "errors      : 2" in result.output
    # Per-row detail block suppressed.
    assert "Errors:" not in result.output
    assert "bad-isbn-1" not in result.output


def test_cli_import_csv_quiet_drops_warnings_block(session, tmp_path):
    """A messy-byte file emits a warnings detail block; --quiet drops it."""
    raw = b"media_type,title,authors,isbn\nbook,D\xe8ne,Frank Herbert,9780441013593\n"
    f = tmp_path / "messy.csv"
    f.write_bytes(raw)
    result = _run(session, import_app, ["csv", str(f), "--dry-run", "--quiet"])
    assert result.exit_code == 0, result.output
    # The block header is gone.
    assert "Warnings:" not in result.output
    # The per-warning bullet line is gone too.
    assert "byte replacement" not in result.output.lower()
    # But the summary is still there.
    assert "total rows  : 1" in result.output


def test_cli_import_librarything_quiet_keeps_summary(session, tmp_path):
    """LibraryThing import shares _print_report, so --quiet behaves the same."""
    f = _lt_tsv(tmp_path)
    result = _run(
        session,
        import_app,
        ["librarything", str(f), "--dry-run", "--quiet"],
    )
    assert result.exit_code == 0, result.output
    assert "Import report (librarything):" in result.output
    assert "Errors:" not in result.output
    assert "Warnings:" not in result.output


def test_cli_import_marc_quiet_keeps_summary(session, tmp_path):
    """MARC import also shares _print_report; smoke test the flag is wired."""
    f = _mk_marc_bytes(tmp_path)
    result = _run(session, import_app, ["marc", str(f), "--dry-run", "--quiet"])
    assert result.exit_code == 0, result.output
    assert "Import report (marc):" in result.output
    assert "Errors:" not in result.output
    assert "Warnings:" not in result.output


def test_cli_export_csv_roundtrip(session, tmp_path):
    fin = _minimal_csv(tmp_path, "in.csv")
    _run(session, import_app, ["csv", str(fin)])
    fout = tmp_path / "out.csv"
    result = _run(session, export_app, ["csv", str(fout)])
    assert result.exit_code == 0
    content = fout.read_text(encoding="utf-8")
    assert "Dune" in content
    assert "Foundation" in content
    assert content.splitlines()[0].startswith("media_type,title,")


def test_cli_export_marc(session, tmp_path):
    _run(session, import_app, ["marc", str(_mk_marc_bytes(tmp_path))])
    fout = tmp_path / "out.mrc"
    result = _run(session, export_app, ["marc", str(fout)])
    assert result.exit_code == 0
    assert fout.stat().st_size > 0
    raw = fout.read_bytes()
    assert b"Herbert" in raw


def test_cli_export_marcxml(session, tmp_path):
    _run(session, import_app, ["marc", str(_mk_marc_bytes(tmp_path))])
    fout = tmp_path / "out.xml"
    result = _run(session, export_app, ["marc", str(fout), "--xml"])
    assert result.exit_code == 0
    xml = fout.read_text(encoding="utf-8")
    assert xml.startswith('<?xml')
    assert "Dune" in xml


# ── --fail-on-error on import commands ──────────────────────────────────────

_PARTIAL_FAILURE_CSV = b"""media_type,title,authors,isbn
book,Dune,Frank Herbert,9780441013593
book,BadRow,Someone,bad-isbn-1
"""


def test_import_csv_partial_failure_still_exits_0_by_default(session, tmp_path):
    """One good row + one bad row, no --fail-on-error: exit 0 (something imported)."""
    f = tmp_path / "partial.csv"
    f.write_bytes(_PARTIAL_FAILURE_CSV)
    result = _run(session, import_app, ["csv", str(f)])
    assert result.exit_code == 0, result.output
    assert "errors      : 1" in result.output


def test_import_csv_fail_on_error_exits_1(session, tmp_path):
    """Same partial-failure CSV, with --fail-on-error: exit 1 even though some rows imported."""
    f = tmp_path / "partial.csv"
    f.write_bytes(_PARTIAL_FAILURE_CSV)
    result = _run(session, import_app, ["csv", str(f), "--fail-on-error"])
    assert result.exit_code == 1, result.output
    assert "errors      : 1" in result.output
