from __future__ import annotations

import tomllib
from pathlib import Path

from compendium.services import scaffold

_REPO = Path(__file__).resolve().parents[2]


def test_force_include_maps_docker_into_package():
    data = tomllib.loads((_REPO / "pyproject.toml").read_text())
    fi = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert fi.get("docker") == "compendium/_scaffold"


def test_dockerfile_builds_non_editable():
    text = (_REPO / "docker" / "Dockerfile").read_text()
    assert "--no-editable" in text, "image must build a real wheel for force-include"


def test_manifest_paths_exist_in_repo_docker():
    base = _REPO / "docker"
    for rel in scaffold.SCAFFOLD_FILES + [scaffold.ENV_EXAMPLE]:
        assert (base / rel).is_file(), rel
