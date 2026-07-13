# tests/integration/test_cli_verb_conventions.py
"""Walk the whole CLI tree: no VISIBLE command may use a banned verb.

Guards against future drift. Hidden aliases are exempt by construction.
"""
import click
import typer.main

from compendium.cli.main import app

BANNED = {"create", "update", "rename"}
# KV-store value-by-key writes are legitimately 'set' / other whitelisted paths:
WHITELIST = {
    ("settings", "set"),
    ("secrets", "set"),
    ("calendar", "hours", "set"),
}


def _walk(cmd: click.Command, path: tuple[str, ...] = ()):
    if isinstance(cmd, click.Group):
        for name, sub in cmd.commands.items():
            if getattr(sub, "hidden", False):
                continue
            yield from _walk(sub, path + (name,))
    else:
        yield path


def test_no_visible_banned_verbs():
    root = typer.main.get_command(app)
    violations = []
    for path in _walk(root):
        leaf = path[-1]
        if path in WHITELIST:
            continue
        if leaf in BANNED or (leaf == "set" or leaf.startswith("set-")):
            violations.append(" ".join(path))
    assert not violations, f"non-canonical visible verbs: {violations}"
