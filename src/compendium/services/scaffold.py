"""Scaffold a ready-to-run Docker deployment bundle.

The canonical bundle lives in the repo `docker/` tree; hatchling force-include
ships it into the wheel at `compendium/_scaffold`. `bundle_base()` resolves
whichever copy is available so `compendium init` works from a PyPI install, the
container image, or an editable source checkout.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass
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


_LOCAL_CNS = frozenset({"compendium.local", "localhost"})


@dataclass
class ScaffoldResult:
    directory: Path
    admin_username: str
    admin_password: str
    admin_password_generated: bool
    cert_cn: str
    using_supplied_cert: bool
    secret_key_enabled: bool


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def scaffold(
    directory: Path,
    *,
    force: bool = False,
    admin_username: str = "admin",
    admin_password: str | None = None,
    db_password: str | None = None,
    cert_cn: str = "compendium.local",
    image: str | None = None,
    with_secret_key: bool = True,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
) -> ScaffoldResult:
    """Write a ready-to-run Docker deployment bundle into ``directory``."""
    directory = Path(directory)

    if bool(tls_cert) != bool(tls_key):
        raise ScaffoldError("provide both --tls-cert and --tls-key, or neither")
    if tls_cert is not None:
        for label, p in (("--tls-cert", tls_cert), ("--tls-key", tls_key)):
            if not Path(p).is_file():
                raise ScaffoldError(f"{label} path does not exist: {p}")

    base = bundle_base()

    targets = [directory / rel for rel in SCAFFOLD_FILES] + [directory / ".env"]
    if not force:
        existing = [t for t in targets if t.exists()]
        if existing:
            names = ", ".join(str(t.relative_to(directory)) for t in existing)
            raise ScaffoldError(
                f"{directory} already contains bundle files ({names}). "
                f"Use --force to overwrite."
            )

    directory.mkdir(parents=True, exist_ok=True)
    for d in SCAFFOLD_DIRS:
        (directory / d).mkdir(parents=True, exist_ok=True)

    for rel in SCAFFOLD_FILES:
        dest = directory / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(base / rel, dest)
        if rel in EXECUTABLE_FILES:
            _make_executable(dest)

    from cryptography.fernet import Fernet

    admin_password_generated = admin_password is None
    if admin_password is None:
        admin_password = secrets.token_urlsafe(12)
    if db_password is None:
        db_password = secrets.token_urlsafe(24)

    values: dict[str, str] = {
        "POSTGRES_PASSWORD": db_password,
        "COMPENDIUM_JWT_SECRET_KEY": secrets.token_urlsafe(48),
        "COMPENDIUM_ADMIN_USERNAME": admin_username,
        "COMPENDIUM_ADMIN_PASSWORD": admin_password,
        "COMPENDIUM_CERT_CN": cert_cn,
    }
    if with_secret_key:
        values["COMPENDIUM_SECRET_KEY"] = Fernet.generate_key().decode()
    if image:
        values["COMPENDIUM_IMAGE"] = image
    if cert_cn not in _LOCAL_CNS:
        values["COMPENDIUM_ALLOWED_HOSTS"] = cert_cn
        values["COMPENDIUM_PUBLIC_BASE_URL"] = f"https://{cert_cn}"

    example_text = (base / ENV_EXAMPLE).read_text()
    env_path = directory / ".env"
    env_path.write_text(render_env(example_text, values))
    env_path.chmod(0o600)

    using_supplied_cert = tls_cert is not None
    if using_supplied_cert:
        certs_dir = directory / "certs"
        shutil.copyfile(tls_cert, certs_dir / "fullchain.pem")
        key_dest = certs_dir / "privkey.pem"
        shutil.copyfile(tls_key, key_dest)
        key_dest.chmod(0o600)

    return ScaffoldResult(
        directory=directory,
        admin_username=admin_username,
        admin_password=admin_password,
        admin_password_generated=admin_password_generated,
        cert_cn=cert_cn,
        using_supplied_cert=using_supplied_cert,
        secret_key_enabled=with_secret_key,
    )
