"""Helpers for using ``-`` as stdin/stdout in CLI file arguments.

All openers route binary content through ``sys.stdin.buffer`` / ``sys.stdout.buffer``
so Windows doesn't translate ``\\n`` ↔ ``\\r\\n`` on us. Text content uses the
default text streams.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import IO, Iterator

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
