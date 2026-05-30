# tests/unit/test_curated_lists_service.py
"""Unit: CuratedListService business logic with mock repos."""
import pytest
from unittest.mock import MagicMock

from compendium.domain.errors import BusinessRuleError, NotFoundError, ValidationError
from compendium.domain.models import CuratedList, CuratedListEntry
from compendium.services.curated_lists import CuratedListService, _MISSING, _make_slug


def _make_list(
    list_id: int,
    name: str,
    slug: str,
    entries: list | None = None,
) -> CuratedList:
    cl = CuratedList(name=name, slug=slug)
    cl.id = list_id
    cl.description = None
    cl.is_public = True
    cl.is_featured = False
    cl.display_order = 0
    if entries is not None:
        cl.entries = entries
    return cl


def _make_entry(
    list_id: int,
    work_id: int,
    order: int = 0,
    annotation: str | None = None,
) -> CuratedListEntry:
    e = CuratedListEntry(list_id=list_id, work_id=work_id)
    e.display_order = order
    e.annotation = annotation
    return e


def _svc(**overrides) -> CuratedListService:
    defaults = dict(
        curated_list_repo=MagicMock(),
        work_repo=MagicMock(),
    )
    defaults.update(overrides)
    return CuratedListService(**defaults)


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------

class TestMakeSlug:
    def test_basic_name(self):
        assert _make_slug("Staff Picks") == "staff-picks"

    def test_special_chars(self):
        assert _make_slug("Best of 2024!") == "best-of-2024"

    def test_multiple_spaces(self):
        assert _make_slug("  Hello   World  ") == "hello-world"

    def test_collapses_hyphens(self):
        assert _make_slug("a -- b") == "a-b"

    def test_max_96_chars(self):
        long_name = "a" * 200
        assert len(_make_slug(long_name)) <= 96

    def test_strips_leading_trailing_hyphens(self):
        assert _make_slug("!hello!") == "hello"


# ---------------------------------------------------------------------------
# Slug deduplication
# ---------------------------------------------------------------------------

class TestSlugDeduplication:
    def test_no_collision_returns_base(self):
        svc = _svc()
        svc._lists.slug_exists.return_value = False
        saved = _make_list(1, "Staff Picks", "staff-picks")
        svc._lists.add.return_value = saved
        result = svc.create("Staff Picks")
        assert result.slug == "staff-picks"

    def test_collision_appends_2(self):
        svc = _svc()
        # "staff-picks" exists, "staff-picks-2" does not
        svc._lists.slug_exists.side_effect = lambda s: s == "staff-picks"
        saved = _make_list(2, "Staff Picks", "staff-picks-2")
        svc._lists.add.return_value = saved
        result = svc.create("Staff Picks")
        assert result.slug == "staff-picks-2"

    def test_double_collision_appends_3(self):
        svc = _svc()
        svc._lists.slug_exists.side_effect = lambda s: s in ("staff-picks", "staff-picks-2")
        saved = _make_list(3, "Staff Picks", "staff-picks-3")
        svc._lists.add.return_value = saved
        result = svc.create("Staff Picks")
        assert result.slug == "staff-picks-3"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

class TestCreate:
    def test_creates_list_with_correct_fields(self):
        svc = _svc()
        svc._lists.slug_exists.return_value = False
        saved = _make_list(1, "New Arrivals", "new-arrivals")
        saved.description = "Fresh titles"
        saved.is_public = True
        saved.is_featured = True
        saved.display_order = 5
        svc._lists.add.return_value = saved
        result = svc.create(
            "New Arrivals",
            description="Fresh titles",
            is_public=True,
            is_featured=True,
            display_order=5,
        )
        svc._lists.add.assert_called_once()
        assert result.name == "New Arrivals"
        assert result.slug == "new-arrivals"

    def test_records_create_audit(self):
        audit = MagicMock()
        svc = _svc(audit_svc=audit)
        svc._lists.slug_exists.return_value = False
        saved = _make_list(1, "Staff Picks", "staff-picks")
        svc._lists.add.return_value = saved
        svc.create("Staff Picks")
        audit.record.assert_called_once()
        _, kwargs = audit.record.call_args
        assert kwargs["action"] == "create"
        assert kwargs["entity_type"] == "curated_list"

    def test_blank_name_raises_validation_error(self):
        svc = _svc()
        with pytest.raises(ValidationError, match="name"):
            svc.create("   ")

    def test_empty_name_raises_validation_error(self):
        svc = _svc()
        with pytest.raises(ValidationError):
            svc.create("")


# ---------------------------------------------------------------------------
# Get / get_by_slug
# ---------------------------------------------------------------------------

class TestGet:
    def test_returns_list(self):
        svc = _svc()
        cl = _make_list(5, "Test", "test")
        svc._lists.get.return_value = cl
        assert svc.get(5) is cl

    def test_not_found_raises(self):
        svc = _svc()
        svc._lists.get.return_value = None
        with pytest.raises(NotFoundError):
            svc.get(999)


class TestGetBySlug:
    def test_returns_list(self):
        svc = _svc()
        cl = _make_list(1, "Staff Picks", "staff-picks")
        svc._lists.get_by_slug.return_value = cl
        assert svc.get_by_slug("staff-picks") is cl

    def test_not_found_raises(self):
        svc = _svc()
        svc._lists.get_by_slug.return_value = None
        with pytest.raises(NotFoundError):
            svc.get_by_slug("no-such-slug")


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

class TestUpdate:
    def test_updates_name(self):
        svc = _svc()
        cl = _make_list(1, "Old Name", "old-name")
        svc._lists.get.return_value = cl
        svc._lists.update.return_value = cl
        svc._lists.slug_exists.return_value = False
        svc.update(1, name="New Name")
        assert cl.name == "New Name"

    def test_missing_sentinel_preserves_fields(self):
        svc = _svc()
        cl = _make_list(1, "Keep This", "keep-this")
        cl.description = "original desc"
        svc._lists.get.return_value = cl
        svc._lists.update.return_value = cl
        svc.update(1, name=_MISSING)
        assert cl.name == "Keep This"
        assert cl.description == "original desc"

    def test_records_update_audit(self):
        audit = MagicMock()
        svc = _svc(audit_svc=audit)
        cl = _make_list(1, "Old", "old")
        svc._lists.get.return_value = cl
        svc._lists.update.return_value = cl
        svc._lists.slug_exists.return_value = False
        svc.update(1, name="New")
        audit.record.assert_called_once()
        _, kwargs = audit.record.call_args
        assert kwargs["action"] == "update"

    def test_blank_name_raises(self):
        svc = _svc()
        svc._lists.get.return_value = _make_list(1, "X", "x")
        with pytest.raises(ValidationError, match="name"):
            svc.update(1, name="  ")

    def test_updates_is_public(self):
        svc = _svc()
        cl = _make_list(1, "Test", "test")
        cl.is_public = True
        svc._lists.get.return_value = cl
        svc._lists.update.return_value = cl
        svc.update(1, is_public=False)
        assert cl.is_public is False


# ---------------------------------------------------------------------------
# Update name regenerates slug
# ---------------------------------------------------------------------------

class TestUpdateNameRegeneratesSlug:
    def test_name_change_regenerates_slug(self):
        svc = _svc()
        cl = _make_list(1, "Old Name", "old-name")
        svc._lists.get.return_value = cl
        svc._lists.update.return_value = cl
        svc._lists.slug_exists.return_value = False
        svc.update(1, name="New Name")
        assert cl.slug == "new-name"

    def test_name_change_slug_deduped(self):
        svc = _svc()
        cl = _make_list(1, "Old", "old")
        # "new-name" exists for a different list (id=99)
        other_list = _make_list(99, "Other", "new-name")
        svc._lists.get.return_value = cl
        svc._lists.update.return_value = cl
        svc._lists.slug_exists.side_effect = lambda s: s == "new-name"
        svc._lists.get_by_slug.side_effect = lambda s: other_list if s == "new-name" else None
        svc.update(1, name="New Name")
        assert cl.slug == "new-name-2"

    def test_explicit_slug_not_overridden_by_name_change(self):
        svc = _svc()
        cl = _make_list(1, "Old", "old")
        svc._lists.get.return_value = cl
        svc._lists.update.return_value = cl
        svc._lists.slug_exists.return_value = False
        svc.update(1, name="New Name", slug="my-custom-slug")
        assert cl.slug == "my-custom-slug"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDelete:
    def test_deletes_list(self):
        svc = _svc()
        cl = _make_list(1, "To Delete", "to-delete")
        svc._lists.get.return_value = cl
        svc.delete(1)
        svc._lists.delete.assert_called_once_with(cl)

    def test_records_delete_audit(self):
        audit = MagicMock()
        svc = _svc(audit_svc=audit)
        cl = _make_list(1, "To Delete", "to-delete")
        svc._lists.get.return_value = cl
        svc.delete(1)
        audit.record.assert_called_once()
        _, kwargs = audit.record.call_args
        assert kwargs["action"] == "delete"

    def test_not_found_raises(self):
        svc = _svc()
        svc._lists.get.return_value = None
        with pytest.raises(NotFoundError):
            svc.delete(99)


# ---------------------------------------------------------------------------
# Add work
# ---------------------------------------------------------------------------

class TestAddWork:
    def test_creates_entry_with_correct_order(self):
        svc = _svc()
        cl = _make_list(1, "Staff Picks", "staff-picks")
        svc._lists.get.return_value = cl
        work = MagicMock()
        work.id = 10
        svc._works.get.return_value = work
        svc._lists.get_entry.return_value = None
        svc._lists.max_entry_order.return_value = 2
        entry = _make_entry(1, 10, order=3)
        svc._lists.add_entry.return_value = entry
        result = svc.add_work(1, 10)
        assert result.list_id == 1
        assert result.work_id == 10
        assert result.display_order == 3

    def test_records_list_add_work_audit(self):
        audit = MagicMock()
        svc = _svc(audit_svc=audit)
        cl = _make_list(1, "List", "list")
        svc._lists.get.return_value = cl
        work = MagicMock()
        work.id = 5
        svc._works.get.return_value = work
        svc._lists.get_entry.return_value = None
        svc._lists.max_entry_order.return_value = 0
        svc._lists.add_entry.return_value = _make_entry(1, 5, order=1)
        svc.add_work(1, 5)
        audit.record.assert_called_once()
        _, kwargs = audit.record.call_args
        assert kwargs["action"] == "list_add_work"

    def test_duplicate_work_raises_business_rule_error(self):
        svc = _svc()
        cl = _make_list(1, "List", "list")
        svc._lists.get.return_value = cl
        work = MagicMock()
        work.id = 7
        svc._works.get.return_value = work
        svc._lists.get_entry.return_value = _make_entry(1, 7)  # already exists
        with pytest.raises(BusinessRuleError, match="already in this list"):
            svc.add_work(1, 7)

    def test_missing_work_raises_not_found(self):
        svc = _svc()
        cl = _make_list(1, "List", "list")
        svc._lists.get.return_value = cl
        svc._works.get.return_value = None
        with pytest.raises(NotFoundError):
            svc.add_work(1, 999)

    def test_missing_list_raises_not_found(self):
        svc = _svc()
        svc._lists.get.return_value = None
        with pytest.raises(NotFoundError):
            svc.add_work(99, 1)


# ---------------------------------------------------------------------------
# Remove work
# ---------------------------------------------------------------------------

class TestRemoveWork:
    def test_removes_entry(self):
        svc = _svc()
        cl = _make_list(1, "List", "list")
        svc._lists.get.return_value = cl
        svc._lists.get_entry.return_value = _make_entry(1, 5)
        svc.remove_work(1, 5)
        svc._lists.remove_entry.assert_called_once_with(1, 5)

    def test_records_list_remove_work_audit(self):
        audit = MagicMock()
        svc = _svc(audit_svc=audit)
        cl = _make_list(1, "List", "list")
        svc._lists.get.return_value = cl
        svc._lists.get_entry.return_value = _make_entry(1, 5)
        svc.remove_work(1, 5)
        audit.record.assert_called_once()
        _, kwargs = audit.record.call_args
        assert kwargs["action"] == "list_remove_work"

    def test_work_not_in_list_raises_not_found(self):
        svc = _svc()
        cl = _make_list(1, "List", "list")
        svc._lists.get.return_value = cl
        svc._lists.get_entry.return_value = None
        with pytest.raises(NotFoundError, match="Work not in this list"):
            svc.remove_work(1, 999)


# ---------------------------------------------------------------------------
# Reorder
# ---------------------------------------------------------------------------

class TestReorder:
    def _make_svc_with_entries(self, entry_work_ids: list[int]):
        svc = _svc()
        entries = [_make_entry(1, wid, order=i) for i, wid in enumerate(entry_work_ids)]
        cl = _make_list(1, "List", "list", entries=entries)
        svc._lists.get.return_value = cl
        entry_map = {e.work_id: e for e in entries}
        svc._lists.get_entry.side_effect = lambda lid, wid: entry_map.get(wid)
        svc._lists.update_entry.side_effect = lambda e: e
        return svc, cl

    def test_reassigns_display_order(self):
        svc, cl = self._make_svc_with_entries([10, 20, 30])
        svc.reorder(1, [30, 10, 20])
        update_calls = svc._lists.update_entry.call_args_list
        assert len(update_calls) == 3
        updated = [c.args[0] for c in update_calls]
        order_by_work = {e.work_id: e.display_order for e in updated}
        assert order_by_work[30] == 0
        assert order_by_work[10] == 1
        assert order_by_work[20] == 2

    def test_returns_list(self):
        svc, cl = self._make_svc_with_entries([10, 20])
        result = svc.reorder(1, [20, 10])
        assert result is cl

    def test_unknown_work_id_raises_business_rule_error(self):
        svc, _ = self._make_svc_with_entries([10, 20])
        with pytest.raises(BusinessRuleError):
            svc.reorder(1, [10, 20, 99])


# ---------------------------------------------------------------------------
# Set annotation
# ---------------------------------------------------------------------------

class TestSetAnnotation:
    def test_updates_annotation(self):
        svc = _svc()
        cl = _make_list(1, "List", "list")
        svc._lists.get.return_value = cl
        entry = _make_entry(1, 5, annotation=None)
        svc._lists.get_entry.return_value = entry
        svc._lists.update_entry.return_value = entry
        result = svc.set_annotation(1, 5, "A classic.")
        assert result.annotation == "A classic."
        svc._lists.update_entry.assert_called_once_with(entry)

    def test_clears_annotation(self):
        svc = _svc()
        cl = _make_list(1, "List", "list")
        svc._lists.get.return_value = cl
        entry = _make_entry(1, 5, annotation="Old text")
        svc._lists.get_entry.return_value = entry
        svc._lists.update_entry.return_value = entry
        result = svc.set_annotation(1, 5, None)
        assert result.annotation is None

    def test_work_not_in_list_raises_not_found(self):
        svc = _svc()
        cl = _make_list(1, "List", "list")
        svc._lists.get.return_value = cl
        svc._lists.get_entry.return_value = None
        with pytest.raises(NotFoundError):
            svc.set_annotation(1, 999, "text")


# ---------------------------------------------------------------------------
# New tests for fixes
# ---------------------------------------------------------------------------

class TestSlugOverflow:
    def test_dedup_with_96_char_base_does_not_exceed_96_chars(self):
        svc = _svc()
        # base slug will be exactly 96 'a' chars
        long_name = "a" * 200
        # first candidate ("aaa...96") exists, "aaa...-2" should still be <=96
        svc._lists.slug_exists.side_effect = lambda s: s == "a" * 96
        saved = _make_list(1, "a" * 200, "a" * 93 + "-2")
        svc._lists.add.return_value = saved
        svc.create(long_name)
        # Verify the candidate passed to add() had slug <= 96 chars
        add_call = svc._lists.add.call_args
        added_cl = add_call.args[0] if add_call.args else add_call[0][0]
        assert len(added_cl.slug) <= 96


class TestEmptySlugFromName:
    def test_all_special_chars_raises_validation_error(self):
        svc = _svc()
        with pytest.raises(ValidationError, match="at least one letter or digit"):
            svc.create("!!!")

    def test_update_all_special_char_name_raises_validation_error(self):
        svc = _svc()
        svc._lists.get.return_value = _make_list(1, "X", "x")
        with pytest.raises(ValidationError, match="at least one letter or digit"):
            svc.update(1, name="!!!")


class TestReorderReturnsSorted:
    def test_reorder_returns_entries_sorted_by_new_display_order(self):
        svc = _svc()
        entries = [_make_entry(1, wid, order=i) for i, wid in enumerate([10, 20, 30])]
        cl = _make_list(1, "List", "list", entries=entries)
        svc._lists.get.return_value = cl
        entry_map = {e.work_id: e for e in entries}
        svc._lists.get_entry.side_effect = lambda lid, wid: entry_map.get(wid)
        svc._lists.update_entry.side_effect = lambda e: e
        result = svc.reorder(1, [30, 10, 20])
        assert [e.work_id for e in result.entries] == [30, 10, 20]

    def test_empty_reorder_works_without_error(self):
        svc = _svc()
        cl = _make_list(1, "List", "list", entries=[])
        svc._lists.get.return_value = cl
        result = svc.reorder(1, [])
        assert result is cl


class TestUpdateExplicitSlugValidation:
    def test_explicit_all_special_slug_raises_validation_error(self):
        svc = _svc()
        svc._lists.get.return_value = _make_list(1, "X", "x")
        with pytest.raises(ValidationError, match="at least one letter or digit"):
            svc.update(1, slug="!!!")

    def test_explicit_slug_is_normalized(self):
        svc = _svc()
        cl = _make_list(1, "X", "x")
        svc._lists.get.return_value = cl
        svc._lists.update.return_value = cl
        svc.update(1, slug="  My Custom Slug!  ")
        assert cl.slug == "my-custom-slug"
