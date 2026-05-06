from importlib.resources import files as _pkg_files
from pathlib import Path

import typer
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from compendium.config.seed import seed_defaults
from compendium.db.engine import get_settings
from compendium.db.session import session_scope

app = typer.Typer(help="Database management commands.")

_MIGRATIONS_DIR = Path(str(_pkg_files("compendium") / "migrations"))


def _alembic_cfg() -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


@app.command()
def init() -> None:
    """Apply all migrations and seed default data (safe to run multiple times)."""
    typer.echo("Applying migrations…")
    alembic_command.upgrade(_alembic_cfg(), "head")

    typer.echo("Seeding default data…")
    with session_scope() as session:
        seed_defaults(session)

    typer.echo(f"Done. Database: {get_settings().database_url}")


@app.command()
def upgrade() -> None:
    """Apply any pending migrations (alias for 'alembic upgrade head')."""
    alembic_command.upgrade(_alembic_cfg(), "head")
    typer.echo("Migrations applied.")


@app.command()
def history() -> None:
    """Show migration history."""
    alembic_command.history(_alembic_cfg())
