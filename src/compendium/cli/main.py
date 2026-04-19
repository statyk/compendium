from typing import Optional

import typer

from compendium.cli.commands import audit, db, hold, item, loan, maintenance, patron, policy, role, user

app = typer.Typer(
    name="compendium",
    help="Compendium — a library card catalog system for physical items.",
    no_args_is_help=True,
)

app.add_typer(audit.app, name="audit")
app.add_typer(db.app, name="db")
app.add_typer(item.app, name="item")
app.add_typer(patron.app, name="patron")
app.add_typer(loan.app, name="loan")
app.add_typer(hold.app, name="hold")
app.add_typer(policy.app, name="policy")
app.add_typer(role.app, name="role")
app.add_typer(maintenance.app, name="maintenance")
app.add_typer(user.app, name="user")


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host"),
    port: int = typer.Option(8000, "--port", help="Bind port"),
    ssl_certfile: Optional[str] = typer.Option(None, "--ssl-certfile", help="Path to TLS certificate file (PEM)"),
    ssl_keyfile: Optional[str] = typer.Option(None, "--ssl-keyfile", help="Path to TLS private key file (PEM)"),
) -> None:
    """Start the HTTP API server."""
    import uvicorn

    from compendium.api.app import create_app
    from compendium.db.engine import get_settings

    s = get_settings()
    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        ssl_certfile=ssl_certfile or s.ssl_certfile or None,
        ssl_keyfile=ssl_keyfile or s.ssl_keyfile or None,
    )
