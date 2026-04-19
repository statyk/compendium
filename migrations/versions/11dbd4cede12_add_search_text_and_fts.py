"""add_search_text_and_fts

Revision ID: 11dbd4cede12
Revises: b3c4d5e6f7a8
Create Date: 2026-04-19 03:43:16.572619

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '11dbd4cede12'
down_revision: Union[str, Sequence[str], None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('work', schema=None) as batch_op:
        batch_op.add_column(sa.Column('search_text', sa.Text(), nullable=True))

    bind = op.get_bind()

    # Backfill search_text for existing rows
    work_ids = [r[0] for r in bind.execute(sa.text("SELECT id FROM work")).fetchall()]
    for work_id in work_ids:
        title, subtitle, description = bind.execute(
            sa.text("SELECT title, subtitle, description FROM work WHERE id = :wid"),
            {"wid": work_id},
        ).fetchone() or ("", None, None)
        creators = bind.execute(
            sa.text(
                "SELECT c.display_name FROM creator c"
                " JOIN work_creator wc ON wc.creator_id = c.id"
                " WHERE wc.work_id = :wid"
            ),
            {"wid": work_id},
        ).fetchall()
        parts = [title or "", subtitle or "", description or ""]
        parts += [c[0] for c in creators]
        search_text = " ".join(p.strip() for p in parts if p and p.strip())
        bind.execute(
            sa.text("UPDATE work SET search_text = :st WHERE id = :wid"),
            {"st": search_text, "wid": work_id},
        )

    if bind.dialect.name == "sqlite":
        bind.execute(sa.text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS work_fts"
            " USING fts5(search_text, content='work', content_rowid='id')"
        ))
        # Populate FTS from existing rows
        bind.execute(sa.text(
            "INSERT INTO work_fts(rowid, search_text)"
            " SELECT id, COALESCE(search_text, '') FROM work"
        ))
        bind.execute(sa.text(
            "CREATE TRIGGER work_fts_ai AFTER INSERT ON work BEGIN"
            "  INSERT INTO work_fts(rowid, search_text)"
            "  VALUES (new.id, COALESCE(new.search_text, ''));"
            " END"
        ))
        bind.execute(sa.text(
            "CREATE TRIGGER work_fts_ad AFTER DELETE ON work BEGIN"
            "  INSERT INTO work_fts(work_fts, rowid, search_text)"
            "  VALUES ('delete', old.id, COALESCE(old.search_text, ''));"
            " END"
        ))
        bind.execute(sa.text(
            "CREATE TRIGGER work_fts_au AFTER UPDATE OF search_text ON work BEGIN"
            "  INSERT INTO work_fts(work_fts, rowid, search_text)"
            "  VALUES ('delete', old.id, COALESCE(old.search_text, ''));"
            "  INSERT INTO work_fts(rowid, search_text)"
            "  VALUES (new.id, COALESCE(new.search_text, ''));"
            " END"
        ))

    elif bind.dialect.name == "postgresql":
        bind.execute(sa.text(
            "CREATE INDEX ix_work_search_gin ON work"
            " USING GIN (to_tsvector('english', COALESCE(search_text, '')))"
        ))


def downgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "sqlite":
        bind.execute(sa.text("DROP TRIGGER IF EXISTS work_fts_au"))
        bind.execute(sa.text("DROP TRIGGER IF EXISTS work_fts_ad"))
        bind.execute(sa.text("DROP TRIGGER IF EXISTS work_fts_ai"))
        bind.execute(sa.text("DROP TABLE IF EXISTS work_fts"))
    elif bind.dialect.name == "postgresql":
        bind.execute(sa.text("DROP INDEX IF EXISTS ix_work_search_gin"))

    with op.batch_alter_table('work', schema=None) as batch_op:
        batch_op.drop_column('search_text')
