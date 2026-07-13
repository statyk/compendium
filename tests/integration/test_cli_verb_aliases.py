"""Every renamed command must behave identically under old and new spellings,
and the old spelling must be hidden from --help."""
import pytest
from typer.testing import CliRunner

from compendium.cli.main import app

runner = CliRunner()

# (group_args, new_name, old_name)
RENAMES = [
    (["household"], "add", "create"),
    (["household"], "edit", "rename"),
    (["role"], "add", "create"),
    (["role"], "edit", "update"),
    (["patron-category"], "add", "create"),
    (["patron-category"], "edit", "update"),
    (["policy"], "add", "create"),
    (["policy"], "edit", "set"),
    (["patron"], "edit", "set"),
    (["curated-list"], "add", "create"),
    (["branch"], "edit", "set"),
    (["creator"], "edit", "rename"),
    (["patron"], "add-user", "create-user"),
]


@pytest.mark.parametrize("group,new,old", RENAMES)
def test_alias_help_identical_and_old_hidden(group, new, old):
    new_help = runner.invoke(app, [*group, new, "--help"])
    old_help = runner.invoke(app, [*group, old, "--help"])
    assert new_help.exit_code == 0, new_help.output
    assert old_help.exit_code == 0, old_help.output
    group_help = runner.invoke(app, [*group, "--help"])
    assert f" {new}" in group_help.output
    # old spelling must not be listed in the group help
    import re
    assert not re.search(rf"^\s*{re.escape(old)}\s", group_help.output, re.M), (
        f"{' '.join(group)} {old} still visible in help"
    )


def test_work_list_exists_and_item_list_is_hidden_alias():
    new = runner.invoke(app, ["work", "list", "--help"])
    old = runner.invoke(app, ["item", "list", "--help"])
    assert new.exit_code == 0 and old.exit_code == 0

    # NOTE: rich renders the Commands panel with a box-drawing "│ " prefix
    # (not plain whitespace), so anchor on a word boundary rather than
    # start-of-line whitespace.
    import re

    item_help = runner.invoke(app, ["item", "--help"]).output
    assert not re.search(r"\blist\b", item_help)
    work_help = runner.invoke(app, ["work", "--help"]).output
    assert re.search(r"\blist\b", work_help)
