import typer

from compendium.cli.commands import db, item, loan, patron

app = typer.Typer(
    name="compendium",
    help="Compendium — a library card catalog system for physical items.",
    no_args_is_help=True,
)

app.add_typer(db.app, name="db")
app.add_typer(item.app, name="item")
app.add_typer(patron.app, name="patron")
app.add_typer(loan.app, name="loan")
