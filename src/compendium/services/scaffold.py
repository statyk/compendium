"""Scaffold a ready-to-run Docker deployment bundle.

The canonical bundle lives in the repo `docker/` tree; hatchling force-include
ships it into the wheel at `compendium/_scaffold`. `bundle_base()` resolves
whichever copy is available so `compendium init` works from a PyPI install, the
container image, or an editable source checkout.
"""
from __future__ import annotations

import os
from pathlib import Path

# Files copied verbatim into the target (relative to the bundle base).
# `.env.example` is rendered into `.env` separately (see render_env / scaffold).
SCAFFOLD_FILES: list[str] = [
    "docker-compose.yml",
    "nginx/nginx.conf",
    "nginx/entrypoint.sh",
    "crontab.sample",
    "install-cron.sh",
]
EXECUTABLE_FILES: frozenset[str] = frozenset({"install-cron.sh", "nginx/entrypoint.sh"})
SCAFFOLD_DIRS: list[str] = ["certs", "backups", "logs"]
ENV_EXAMPLE = ".env.example"


class ScaffoldError(Exception):
    """A user-facing scaffold failure (bad input, conflicts, missing bundle)."""


def bundle_base() -> Path:
    """Locate the deployment bundle directory.

    Resolution order: $COMPENDIUM_SCAFFOLD_DIR → packaged `compendium/_scaffold`
    (built wheel / image / PyPI) → the repo `docker/` dir (editable dev/tests).
    """
    override = os.environ.get("COMPENDIUM_SCAFFOLD_DIR")
    if override:
        p = Path(override)
        if not p.is_dir():
            raise ScaffoldError(
                f"COMPENDIUM_SCAFFOLD_DIR={override!r} is not a directory"
            )
        return p

    import compendium

    pkg_dir = Path(compendium.__file__).resolve().parent
    packaged = pkg_dir / "_scaffold"
    if packaged.is_dir():
        return packaged

    for ancestor in pkg_dir.parents:
        candidate = ancestor / "docker"
        if (candidate / "docker-compose.yml").is_file():
            return candidate

    raise ScaffoldError(
        "could not locate the deployment bundle "
        "(neither a packaged _scaffold nor a source docker/ directory)"
    )
