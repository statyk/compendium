"""compendium keygen — generate random secret keys for env configuration."""
from __future__ import annotations

import secrets

import typer

app = typer.Typer(help="Generate random secret keys for environment configuration.")


@app.callback(invoke_without_command=True)
def keygen(
    ctx: typer.Context,
    jwt: bool = typer.Option(False, "--jwt", help="Generate only the JWT secret key."),
    secret: bool = typer.Option(False, "--secret", help="Generate only the encryption secret key."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Omit comments; print bare KEY=value lines."),
) -> None:
    """Print random secret keys ready to paste into your environment.

    By default prints both COMPENDIUM_JWT_SECRET_KEY and COMPENDIUM_SECRET_KEY.
    Pass --jwt or --secret to generate only one.
    """
    if ctx.invoked_subcommand is not None:
        return

    from cryptography.fernet import Fernet

    both = not jwt and not secret

    if not quiet:
        typer.echo("# Add to your environment (.env, systemd unit, docker-compose, etc.):")

    if jwt or both:
        jwt_key = secrets.token_urlsafe(48)
        typer.echo(f"COMPENDIUM_JWT_SECRET_KEY={jwt_key}")

    if secret or both:
        enc_key = Fernet.generate_key().decode()
        typer.echo(f"COMPENDIUM_SECRET_KEY={enc_key}")

    if not quiet and both:
        typer.echo(
            "\n# COMPENDIUM_SECRET_KEY enables encrypted storage of API keys and passwords\n"
            "# in the admin UI (Admin → System → Secrets). Keep both values secure.\n"
            "# Losing COMPENDIUM_SECRET_KEY makes stored secrets unrecoverable."
        )
