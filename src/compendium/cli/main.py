import typer

from compendium.cli.commands import db, hold, item, loan, maintenance, patron, policy, user

app = typer.Typer(
    name="compendium",
    help="Compendium — a library card catalog system for physical items.",
    no_args_is_help=True,
)

app.add_typer(db.app, name="db")
app.add_typer(item.app, name="item")
app.add_typer(patron.app, name="patron")
app.add_typer(loan.app, name="loan")
app.add_typer(hold.app, name="hold")
app.add_typer(policy.app, name="policy")
app.add_typer(maintenance.app, name="maintenance")
app.add_typer(user.app, name="user")


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host"),
    port: int = typer.Option(8000, "--port", help="Bind port"),
) -> None:
    """Start the HTTP API server."""
    import uvicorn

    from compendium.api.app import create_app

    uvicorn.run(create_app(), host=host, port=port)
