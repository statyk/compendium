"""Thin wrapper: ``/ui/covers?url=<upstream>`` → same-origin cover JPEG.

All caching, host-allowlist, and fetch logic lives in
:mod:`compendium.services.covers`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from compendium.services.covers import (
    CoverNotFound,
    DisallowedHost,
    fetch_or_404,
)

router = APIRouter()

_BROWSER_CACHE_SECONDS = 86400


@router.get("/covers")
def proxy_cover(url: str = Query(..., min_length=1)) -> FileResponse:
    try:
        path = fetch_or_404(url)
    except DisallowedHost:
        raise HTTPException(status_code=400, detail="url not allowed")
    except CoverNotFound:
        raise HTTPException(status_code=404, detail="no cover")

    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": f"public, max-age={_BROWSER_CACHE_SECONDS}"},
    )
