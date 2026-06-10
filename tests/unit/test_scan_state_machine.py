"""Unit tests for the phone-scanner state machine and idempotency guard."""

from __future__ import annotations

from types import SimpleNamespace

from compendium.domain.identifiers import format_item_barcode, format_patron_card
from compendium.web.routes.scan import IdempotencyGuard, run_state_machine

ITEM_BC = format_item_barcode("00010001", location_code=None)
PATRON_BC = format_patron_card("00020002", location_code=None)


def _row(mode, *, allowed=None, borrower=None, count=0, catalog_review=False):
    return SimpleNamespace(
        id=1,
        mode=mode,
        allowed_modes=allowed or [mode],
        borrower_patron_id=borrower.id if borrower else None,
        borrower=borrower,
        count=count,
        catalog_review=catalog_review,
    )


def _patron(card=PATRON_BC, name="Jane Doe", pid=42):
    return SimpleNamespace(id=pid, library_card_number=card, full_name=name)


def _loan(title="Dune", item_id=7):
    work = SimpleNamespace(title=title)
    item = SimpleNamespace(id=item_id, work=work)
    return SimpleNamespace(item=item)


class _Repo:
    def __init__(self, patron=None):
        self._patron = patron

    def get_by_card_number(self, card):
        return self._patron


def _call(row, code, *, repo=None, checkout=None, checkin=None, add=None):
    return run_state_machine(
        row,
        code,
        patron_repo=repo or _Repo(),
        checkout=checkout or (lambda *a: _loan()),
        checkin=checkin or (lambda *a: _loan()),
        add_from_isbn=add or (lambda c: (SimpleNamespace(title="Added Title"), None)),
    )


# ── checkout mode ─────────────────────────────────────────────────────────────


def test_checkout_patron_card_sets_borrower():
    p = _patron()
    row = _row("checkout")
    reply = _call(row, PATRON_BC, repo=_Repo(p))
    assert reply["kind"] == "borrower_set"
    assert row.borrower_patron_id == p.id
    assert row.count == 0


def test_checkout_item_without_borrower_errors():
    row = _row("checkout")
    reply = _call(row, ITEM_BC)
    assert reply["kind"] == "error"
    assert "patron card first" in reply["message"].lower()


def test_checkout_item_with_borrower_checks_out_and_increments():
    p = _patron()
    row = _row("checkout", borrower=p, count=2)
    called = {}

    def _checkout(bc, card):
        called["bc"] = bc
        called["card"] = card
        return _loan("TestBook")

    reply = _call(row, ITEM_BC, checkout=_checkout)
    assert reply["kind"] == "checkout"
    assert "TestBook" in reply["message"]
    assert row.count == 3
    assert called == {"bc": ITEM_BC, "card": p.library_card_number}


def test_checkout_unknown_patron_card_errors():
    row = _row("checkout")
    reply = _call(row, PATRON_BC, repo=_Repo(None))
    assert reply["kind"] == "error"


# ── checkin mode ──────────────────────────────────────────────────────────────


def test_checkin_item_checks_in():
    row = _row("checkin")
    reply = _call(row, ITEM_BC, checkin=lambda bc: _loan("Returned"))
    assert reply["kind"] == "checkin"
    assert row.count == 1


def test_checkin_non_item_errors():
    row = _row("checkin")
    reply = _call(row, PATRON_BC)
    assert reply["kind"] == "error"
    assert "item barcode" in reply["message"].lower()


# ── catalog mode ──────────────────────────────────────────────────────────────


def test_catalog_isbn_adds():
    row = _row("catalog")
    reply = _call(row, "9780441013593", add=lambda c: (SimpleNamespace(title="Dune"), None))
    assert reply["kind"] == "catalog_added"
    assert "Dune" in reply["message"]
    assert row.count == 1


def test_catalog_rejects_item_barcode():
    row = _row("catalog")
    reply = _call(row, ITEM_BC)
    assert reply["kind"] == "error"
    assert "not a catalog identifier" in reply["message"].lower()


def test_catalog_rejects_patron_card():
    row = _row("catalog")
    reply = _call(row, PATRON_BC)
    assert reply["kind"] == "error"


def test_catalog_non_isbn_non_barcode_errors_gracefully():
    row = _row("catalog")
    reply = _call(row, "SOMERANDOMUPC123")
    assert reply["kind"] == "error"
    assert "at the desk" in reply["message"].lower()


def test_catalog_review_on_queues_pending_no_item():
    row = _row("catalog", catalog_review=True)
    queued = {}

    def fetch_metadata(code):
        return {"title": "The Giver", "isbn": "9780544336261"}

    def queue_pending(code, meta):
        queued["code"] = code
        queued["meta"] = meta
        return object()

    def add_from_isbn(code):  # must NOT be called in review mode
        raise AssertionError("add_from_isbn called in review mode")

    reply = run_state_machine(
        row,
        "9780544336261",
        patron_repo=None,
        checkout=None,
        checkin=None,
        add_from_isbn=add_from_isbn,
        fetch_metadata=fetch_metadata,
        queue_pending=queue_pending,
    )
    assert reply["ok"] is True
    assert reply["kind"] == "catalog_queued"
    assert "Queued for review" in reply["message"]
    assert queued["meta"]["title"] == "The Giver"
    assert row.count == 1


def test_catalog_review_off_still_adds():
    row = _row("catalog", catalog_review=False)
    added = {}

    def add_from_isbn(code):
        added["code"] = code
        return (type("W", (), {"title": "The Giver"})(), object())

    reply = run_state_machine(
        row,
        "9780544336261",
        patron_repo=None,
        checkout=None,
        checkin=None,
        add_from_isbn=add_from_isbn,
        fetch_metadata=None,
        queue_pending=None,
    )
    assert reply["kind"] == "catalog_added"
    assert added["code"] == "9780544336261"


# ── idempotency guard ─────────────────────────────────────────────────────────


def test_idempotency_guard_collapses_rapid_duplicate():
    clock = {"t": 100.0}
    guard = IdempotencyGuard(window=2.0, clock=lambda: clock["t"])
    assert guard.is_duplicate(1, "checkout", ITEM_BC) is False
    clock["t"] = 100.5
    assert guard.is_duplicate(1, "checkout", ITEM_BC) is True


def test_idempotency_guard_allows_after_window():
    clock = {"t": 100.0}
    guard = IdempotencyGuard(window=2.0, clock=lambda: clock["t"])
    assert guard.is_duplicate(1, "checkout", ITEM_BC) is False
    clock["t"] = 103.0
    assert guard.is_duplicate(1, "checkout", ITEM_BC) is False


def test_idempotency_guard_different_code_not_duplicate():
    clock = {"t": 100.0}
    guard = IdempotencyGuard(window=2.0, clock=lambda: clock["t"])
    assert guard.is_duplicate(1, "checkout", ITEM_BC) is False
    clock["t"] = 100.5
    assert guard.is_duplicate(1, "checkout", PATRON_BC) is False
