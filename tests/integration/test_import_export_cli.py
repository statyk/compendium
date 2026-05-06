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
