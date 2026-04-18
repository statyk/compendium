import typer

from compendium.db.engine import get_settings
from compendium.db.session import session_scope
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.hold_repository import SqlHoldRepository
from compendium.repositories.sql.patron_repository import SqlPatronRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.services.holds import HoldService

app = typer.Typer(help="Maintenance commands (intended for cron/systemd).")


@app.command("expire-holds")
def expire_holds() -> None:
    """Expire waiting holds whose expiry date has passed."""
    with session_scope() as session:
        svc = HoldService(
            hold_repo=SqlHoldRepository(session),
            patron_repo=SqlPatronRepository(session),
            work_repo=SqlWorkRepository(session),
            branch_repo=SqlBranchRepository(session),
            hold_expiry_days=get_settings().hold_expiry_days,
        )
        count = svc.expire_holds()
        typer.echo(f"Expired {count} hold(s).")
