"""Integration tests for recoverable work deletion (trash)."""
from compendium.config.seed import _LIBRARIAN_PERMISSIONS
from compendium.domain.models import DeletedEntity


def test_deleted_entity_round_trip(session):
    row = DeletedEntity(
        entity_type="work",
        entity_id=42,
        label="Dune — 2 copies",
        payload={"version": 1, "work": {"title": "Dune"}},
    )
    session.add(row)
    session.flush()

    got = session.get(DeletedEntity, row.id)
    assert got.entity_type == "work"
    assert got.payload["work"]["title"] == "Dune"
    assert got.deleted_at is not None
    assert got.deleted_by is None


def test_librarian_preset_includes_work_delete():
    assert "work.delete" in _LIBRARIAN_PERMISSIONS
