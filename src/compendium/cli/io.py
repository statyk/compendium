"""Helpers for using ``-`` as stdin/stdout in CLI file arguments.

All openers route binary content through ``sys.stdin.buffer`` / ``sys.stdout.buffer``
so Windows doesn't translate ``\\n`` ↔ ``\\r\\n`` on us. Text content uses the
default text streams.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import IO, Any, Callable, Iterator

import typer

STDIO = "-"


def is_stdio(path: str | Path | None) -> bool:
    if path is None:
        return False
    return str(path) == STDIO


@contextlib.contextmanager
def open_input(path: str | Path, *, binary: bool = True) -> Iterator[IO]:
    """Yield a file-like for reading. When ``path`` is ``-`` reads from stdin."""
    if is_stdio(path):
        yield sys.stdin.buffer if binary else sys.stdin
        return
    mode = "rb" if binary else "r"
    f = open(path, mode)
    try:
        yield f
    finally:
        f.close()


@contextlib.contextmanager
def open_output(path: str | Path, *, binary: bool = True) -> Iterator[IO]:
    """Yield a file-like for writing. When ``path`` is ``-`` writes to stdout."""
    if is_stdio(path):
        yield sys.stdout.buffer if binary else sys.stdout
        return
    mode = "wb" if binary else "w"
    f = open(path, mode)
    try:
        yield f
    finally:
        f.close()


def error(msg: object) -> None:
    """Uniform CLI error line: 'Error: <msg>' in red on stderr."""
    typer.secho(f"Error: {msg}", fg=typer.colors.RED, err=True)


def register_alias(app: typer.Typer, name: str, fn: Callable[..., Any]) -> None:
    """Register an old command spelling as a hidden, permanent alias."""
    app.command(name, hidden=True)(fn)


def resolve_identifier(positional: str | None, option: str | None, *, label: str) -> str:
    """Merge a new positional identifier with its legacy --option fallback."""
    if positional is not None and option is not None and positional != option:
        error(f"pass the {label} either as an argument or via the option, not both")
        raise typer.Exit(2)
    value = positional if positional is not None else option
    if value is None:
        error(f"missing {label}")
        raise typer.Exit(2)
    return value


def truncation_notice(shown: int, limit: int) -> None:
    """Stderr hint when a list result hit --limit (never pollutes stdout)."""
    if shown == limit:
        typer.secho(
            f"Showing first {limit} row(s); more may exist. Raise --limit to see more.",
            err=True,
        )
