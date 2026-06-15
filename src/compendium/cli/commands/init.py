"""compendium init — scaffold a ready-to-run Docker deployment bundle."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from compendium.services.scaffold import ScaffoldError, ScaffoldResult, scaffold


def init_command(
    directory: Path = typer.Argument(
        Path("compendium"),
        help="Target directory for the deployment bundle (created if missing).",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing bundle files."),
    admin_username: str = typer.Option("admin", "--admin-username", help="Initial admin username."),
    admin_password: Optional[str] = typer.Option(
        None, "--admin-password",
        help="Admin password. If omitted, a strong one is generated and PRINTED to the terminal.",
    ),
    db_password: Optional[str] = typer.Option(
        None, "--db-password", help="PostgreSQL password (generated if omitted)."
    ),
    cert_cn: str = typer.Option(
        "compendium.local", "--cert-cn",
        help="Hostname/CN for TLS. A real hostname also sets allowed-hosts and the public base URL.",
    ),
    image: Optional[str] = typer.Option(
        None, "--image", help="Pin the container image (default: latest published)."
    ),
    no_secret_key: bool = typer.Option(
        False, "--no-secret-key", help="Do not generate COMPENDIUM_SECRET_KEY (encrypted-secrets UI)."
    ),
    tls_cert: Optional[Path] = typer.Option(
        None, "--tls-cert", help="CA-signed fullchain PEM to use instead of self-signed (requires --tls-key)."
    ),
    tls_key: Optional[Path] = typer.Option(
        None, "--tls-key", help="Private key PEM (requires --tls-cert)."
    ),
) -> None:
    """Scaffold a ready-to-run Docker deployment into DIRECTORY.

    Writes docker-compose.yml, the nginx config, cron helpers, and a .env with
    freshly generated secrets — so `docker compose up -d` works with no editing.

    NOTE: if --admin-password is omitted, a strong password is generated and
    PRINTED to the terminal so you can log in. Pass --admin-password to choose
    your own and avoid printing it.
    """
    try:
        result = scaffold(
            directory,
            force=force,
            admin_username=admin_username,
            admin_password=admin_password,
            db_password=db_password,
            cert_cn=cert_cn,
            image=image,
            with_secret_key=not no_secret_key,
            tls_cert=tls_cert,
            tls_key=tls_key,
        )
    except ScaffoldError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    _print_summary(result)


def _print_summary(result: ScaffoldResult) -> None:
    d = result.directory
    typer.secho(f"\n✓ Deployment scaffolded in {d}/", fg=typer.colors.GREEN, bold=True)
    typer.echo("")
    typer.echo("Admin login:")
    typer.echo(f"  username: {result.admin_username}")
    if result.admin_password_generated:
        typer.secho(f"  password: {result.admin_password}", fg=typer.colors.YELLOW)
        typer.echo("  (generated — printed above; change it after first login)")
    else:
        typer.echo("  password: (the one you supplied)")
    typer.echo("")
    if result.using_supplied_cert:
        typer.echo("TLS: using your supplied certificate.")
    else:
        typer.echo(
            "TLS: a self-signed certificate will be generated on first start.\n"
            f"     To use a real one, drop fullchain.pem + privkey.pem into {d}/certs/ "
            "before starting (or re-run with --tls-cert/--tls-key)."
        )
    scheme_host = result.cert_cn if result.cert_cn not in ("compendium.local", "localhost") else "localhost"
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo(f"  cd {d}")
    typer.echo("  docker compose up -d")
    typer.echo(f"  open https://{scheme_host}/")
    typer.echo("  ./install-cron.sh        # optional: scheduled maintenance")
