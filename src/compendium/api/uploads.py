"""Bounded reads for file uploads.

Defends bulk-import endpoints against multi-GB request bodies that would
OOM the daemon (M4 from the 2026-04-27 security audit).

Two-layer guard:

1. **Pre-read**: if the request advertises `Content-Length` larger than the
   cap, return 413 immediately — body is never buffered. Honest clients
   pay nothing.

2. **Read-loop**: read the body in 64 KB chunks, abort with 413 once the
   accumulated size exceeds the cap. Catches the chunked-encoding /
   lying-`Content-Length` cases.

The importer service downstream wraps the bytes in `StringIO` / `BytesIO`,
so we still produce in-memory bytes — this fix bounds the worst case, not
the steady-state memory cost of a legitimate large import.
"""

from __future__ import annotations

from fastapi import HTTPException, UploadFile, status

_CHUNK_SIZE = 64 * 1024


def _too_large(cap: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail=f"Upload exceeds maximum allowed size of {cap} bytes.",
    )


async def read_upload_bounded(
    file: UploadFile,
    *,
    cap: int,
    content_length: int | None,
) -> bytes:
    if content_length is not None and content_length > cap:
        raise _too_large(cap)
    buf = bytearray()
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > cap:
            raise _too_large(cap)
    return bytes(buf)
