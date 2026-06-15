"""Scaffold a ready-to-run Docker deployment bundle.

The canonical bundle lives in the repo `docker/` tree; hatchling force-include
ships it into the wheel at `compendium/_scaffold`. `bundle_base()` resolves
whichever copy is available so `compendium init` works from a PyPI install, the
container image, or an editable source checkout.
"""
from __future__ import annotations

import os
import re
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


_ENV_KEY_RE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=")


def render_env(example_text: str, values: dict[str, str]) -> str:
    """Return `.env.example` text with each key in ``values`` set to its value.

    Replaces an existing ``KEY=…`` or ``# KEY=…`` line in place (preserving
    surrounding comments); appends any remaining keys at the end. Keys not in
    ``values`` are left exactly as they were.
    """
    remaining = dict(values)
    out: list[str] = []
    for line in example_text.splitlines():
        m = _ENV_KEY_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    if remaining:
        out.append("")
        out.append("# --- Values set by `compendium init` ---")
        out.extend(f"{k}={v}" for k, v in remaining.items())
    return "\n".join(out) + "\n"


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
