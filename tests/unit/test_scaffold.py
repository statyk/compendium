from __future__ import annotations

import os
from pathlib import Path

import pytest

from compendium.services import scaffold


def test_bundle_base_finds_repo_docker_in_dev():
    base = scaffold.bundle_base()
    assert (base / "docker-compose.yml").is_file()
    assert (base / ".env.example").is_file()
    assert (base / "nginx" / "nginx.conf").is_file()


def test_bundle_base_honors_env_override(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("x")
    monkeypatch.setenv("COMPENDIUM_SCAFFOLD_DIR", str(tmp_path))
    assert scaffold.bundle_base() == tmp_path


def test_bundle_base_env_override_must_be_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPENDIUM_SCAFFOLD_DIR", str(tmp_path / "nope"))
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.bundle_base()


def test_manifest_files_all_exist_in_bundle():
    base = scaffold.bundle_base()
    for rel in scaffold.SCAFFOLD_FILES:
        assert (base / rel).is_file(), rel
    assert (base / scaffold.ENV_EXAMPLE).is_file()
