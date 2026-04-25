"""split_admin_roles

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-04-24 19:30:00.000000

Splits the omnipotent ``Librarian`` preset into three:

- ``Administrator`` — wildcard ``["*"]``, single-person deployments.
- ``Librarian`` — explicit list, no wildcard, no system-tier perms. Day-to-day.
- ``SystemAdmin`` — system.manage + user.manage + role.manage + audit.view +
  minimal view perms. The IT/sysadmin seat for multi-person deployments.

Idempotent:
- Inserts Administrator + SystemAdmin only if absent.
- Rewrites Librarian permissions ONLY if it currently has ``["*"]`` —
  preserves any deployment that customized the preset.
- Adds ``audit.view`` to any role that currently has ``patron.manage`` (the
  old gate for the audit log endpoint), so we don't accidentally lock anyone
  out of the audit log when we swap that gate.
"""
from typing import Sequence, Union
import json

import sqlalchemy as sa
from alembic import op

revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, Sequence[str], None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LIBRARIAN_PERMS = [
    "work.view", "work.edit",
    "item.view", "item.create", "item.edit", "item.delete",
    "catalog.import",
    "loan.checkout", "loan.checkin",
    "loan.renew.any", "loan.renew.self",
    "loan.view.self", "loan.view.any",
    "loan.claim.self",
    "hold.place.self", "hold.place.any",
    "hold.view.self", "hold.view.any",
    "fine.manage", "fine.view.self",
    "notification.manage",
    "report.view",
    "labels.generate",
    "audit.view",
    "patron.manage", "policy.edit", "branch.edit",
]

_SYSTEM_ADMIN_PERMS = [
    "system.manage",
    "user.manage",
    "role.manage",
    "audit.view",
    "item.view", "work.view",
]


def upgrade() -> None:
    bind = op.get_bind()
    role_t = sa.Table(
        "role",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64)),
        sa.Column("permissions", sa.JSON),
        sa.Column("is_system", sa.Boolean),
    )

    rows = list(bind.execute(sa.select(role_t)))

    by_name = {r.name: r for r in rows}

    # --- Rewrite Librarian preset only if still wildcard --------------------
    if "Librarian" in by_name:
        current = by_name["Librarian"].permissions
        # JSON columns may come back as either list or json-string depending
        # on the dialect/configuration. Normalize.
        if isinstance(current, str):
            try:
                current = json.loads(current)
            except json.JSONDecodeError:
                current = []
        if current == ["*"]:
            bind.execute(
                role_t.update()
                .where(role_t.c.id == by_name["Librarian"].id)
                .values(permissions=_LIBRARIAN_PERMS, is_system=True)
            )

    # --- Insert Administrator if absent -------------------------------------
    if "Administrator" not in by_name:
        bind.execute(
            role_t.insert().values(
                name="Administrator",
                permissions=["*"],
                is_system=True,
            )
        )

    # --- Insert SystemAdmin if absent ---------------------------------------
    if "SystemAdmin" not in by_name:
        bind.execute(
            role_t.insert().values(
                name="SystemAdmin",
                permissions=_SYSTEM_ADMIN_PERMS,
                is_system=True,
            )
        )

    # --- Add audit.view to anyone holding patron.manage (old audit gate) ----
    # Skip wildcard roles (they already cover audit.view) and roles that
    # already have audit.view explicitly.
    for r in rows:
        perms = r.permissions
        if isinstance(perms, str):
            try:
                perms = json.loads(perms)
            except json.JSONDecodeError:
                continue
        if not isinstance(perms, list):
            continue
        if "*" in perms or "audit.view" in perms:
            continue
        if "patron.manage" in perms:
            new_perms = perms + ["audit.view"]
            bind.execute(
                role_t.update()
                .where(role_t.c.id == r.id)
                .values(permissions=new_perms)
            )


def downgrade() -> None:
    bind = op.get_bind()
    role_t = sa.Table(
        "role",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64)),
        sa.Column("permissions", sa.JSON),
    )
    bind.execute(role_t.delete().where(role_t.c.name == "Administrator"))
    bind.execute(role_t.delete().where(role_t.c.name == "SystemAdmin"))
    # Restore Librarian wildcard if it was rewritten.
    bind.execute(
        role_t.update()
        .where(role_t.c.name == "Librarian")
        .values(permissions=["*"])
    )
