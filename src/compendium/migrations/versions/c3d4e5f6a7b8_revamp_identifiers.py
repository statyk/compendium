"""revamp_identifiers

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-03 15:00:00.000000

Regenerates every item barcode/accession_number and every patron
library_card_number in the new 10/14-digit format:

    [type][optional 4-digit location][8-digit slug][Luhn mod-10 check]

ITEM_TYPE=3, PATRON_TYPE=2. Item slugs are reassigned sequentially starting
at 1 (id-ordered), ensuring the new namespace (8-digit) cannot collide with
the old (≤6-digit) namespace. Patron slugs are minted with cryptographic
randomness. The catalog.accession counter is reset to the final sequential
slug so that new items never collide.

DEPLOY NOTE: drain the notification outbox before running this migration
    compendium maintenance send-queued-notifications
Queued messages may reference legacy barcodes in their rendered bodies.
Audit log entries that reference legacy barcodes are left as-is (the audit
log is immutable; historical entries remain accurate as of their timestamp).
Physical item labels become unreadable after migration; relabel during the
rollout window.
"""

from __future__ import annotations

import secrets
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ITEM_TYPE = 3
_PATRON_TYPE = 2


def _luhn_check_digit(digits: str) -> int:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return (10 - (total % 10)) % 10


def _format_barcode(type_digit: int, slug: str, location_code: str | None) -> str:
    payload = f"{type_digit}{location_code}{slug}" if location_code else f"{type_digit}{slug}"
    return f"{payload}{_luhn_check_digit(payload)}"


def upgrade() -> None:
    conn = op.get_bind()

    # Read barcode location settings from site_setting table (if present).
    rows = conn.execute(
        sa.text(
            "SELECT key, value FROM site_setting"
            " WHERE key IN ('barcode_location_enabled', 'barcode_default_location_code')"
        )
    ).fetchall()
    ss: dict[str, str] = {r[0]: r[1] for r in rows}
    location_enabled = ss.get("barcode_location_enabled", "false").lower() in (
        "true", "1", "yes",
    )
    default_location = ss.get("barcode_default_location_code", "0000")

    # Build branch_id → location_code lookup.
    branch_rows = conn.execute(sa.text("SELECT id, location_code FROM branch")).fetchall()
    branch_locs: dict[int, str | None] = {r[0]: r[1] for r in branch_rows}

    # Regenerate item barcodes and accession_numbers — sequential slugs, id-ordered.
    item_rows = conn.execute(
        sa.text("SELECT id, branch_id FROM item ORDER BY id")
    ).fetchall()
    slug_counter = 0
    for item_id, branch_id in item_rows:
        slug_counter += 1
        slug = f"{slug_counter:08d}"
        if location_enabled:
            loc = (branch_locs.get(branch_id) if branch_id is not None else None) or default_location
        else:
            loc = None
        barcode = _format_barcode(_ITEM_TYPE, slug, loc)
        conn.execute(
            sa.text("UPDATE item SET barcode = :b, accession_number = :a WHERE id = :i"),
            {"b": barcode, "a": slug, "i": item_id},
        )

    # Reset counter to the final slug value so new items continue from here.
    conn.execute(
        sa.text("UPDATE counters SET value = :v WHERE key = 'catalog.accession'"),
        {"v": slug_counter},
    )

    # Regenerate patron library_card_numbers — random slugs, collision-checked
    # in-process (single transaction; can't query intermediate DB state).
    patron_rows = conn.execute(
        sa.text("SELECT id FROM patron ORDER BY id")
    ).fetchall()
    seen: set[str] = set()
    for (patron_id,) in patron_rows:
        for _ in range(1000):
            patron_slug = f"{secrets.randbelow(10 ** 8):08d}"
            patron_loc = default_location if location_enabled else None
            card = _format_barcode(_PATRON_TYPE, patron_slug, patron_loc)
            if card not in seen:
                seen.add(card)
                break
        else:
            raise RuntimeError(
                f"Could not mint a unique patron card after 1000 attempts (patron id={patron_id})."
            )
        conn.execute(
            sa.text("UPDATE patron SET library_card_number = :c WHERE id = :i"),
            {"c": card, "i": patron_id},
        )


def downgrade() -> None:
    raise NotImplementedError(
        "revamp_identifiers cannot be reversed: barcodes were regenerated and "
        "original values are not preserved. See release notes for the rollback procedure."
    )
