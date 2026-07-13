import typer

from compendium.db.session import session_scope
from compendium.domain.errors import DomainError, NotFoundError
from compendium.repositories.sql.branch_repository import SqlBranchRepository
from compendium.repositories.sql.creator_repository import SqlCreatorRepository
from compendium.repositories.sql.item_repository import SqlItemRepository
from compendium.repositories.sql.media_type_repository import SqlMediaTypeRepository
from compendium.repositories.sql.work_repository import SqlWorkRepository
from compendium.repositories.sql.counters import SqlCounterRepository
from compendium.services.catalog import CatalogService

app = typer.Typer(
    help=(
        "Creator (author/director/artist) commands. "
        "Operates on the global Creator table. "
        "For per-work add/remove/reorder, see 'compendium work creator'."
    )
)


def _catalog(session):
    return CatalogService(
        work_repo=SqlWorkRepository(session),
        item_repo=SqlItemRepository(session),
        creator_repo=SqlCreatorRepository(session),
        branch_repo=SqlBranchRepository(session),
        media_type_repo=SqlMediaTypeRepository(session),
        counter_repo=SqlCounterRepository(session),
    )


@app.command("edit")
def rename_creator(
    creator_id: int = typer.Option(..., "--id", help="Creator ID."),
    display_name: str = typer.Option(..., "--name", help="New display name."),
) -> None:
    """Edit a Creator row's display name. Affects every work this creator appears on."""
    try:
        with session_scope() as session:
            creator = _catalog(session).update_creator(
                creator_id, display_name=display_name
            )
            typer.echo(f"Renamed to: {creator.display_name}  ({creator.sort_name})")
    except NotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except DomainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


from compendium.cli.io import register_alias  # noqa: E402

register_alias(app, "rename", rename_creator)
