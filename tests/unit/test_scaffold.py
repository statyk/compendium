from __future__ import annotations

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


def test_render_env_replaces_existing_key():
    example = "# comment\nPOSTGRES_PASSWORD=change-me-postgres\nHTTP_PORT=80\n"
    out = scaffold.render_env(example, {"POSTGRES_PASSWORD": "s3cret"})
    assert "POSTGRES_PASSWORD=s3cret" in out
    assert "change-me-postgres" not in out
    assert "# comment" in out          # comments preserved
    assert "HTTP_PORT=80" in out       # untouched keys preserved


def test_render_env_uncomments_commented_key():
    example = "# COMPENDIUM_SECRET_KEY=\n"
    out = scaffold.render_env(example, {"COMPENDIUM_SECRET_KEY": "abc"})
    assert "COMPENDIUM_SECRET_KEY=abc" in out
    assert out.count("COMPENDIUM_SECRET_KEY=") == 1


def test_render_env_appends_missing_key():
    out = scaffold.render_env("HTTP_PORT=80\n", {"NEW_KEY": "v"})
    assert "NEW_KEY=v" in out


def test_render_env_leaves_unlisted_commented_key_commented():
    example = "# COMPENDIUM_SECRET_KEY=\n"
    out = scaffold.render_env(example, {})
    assert "# COMPENDIUM_SECRET_KEY=" in out
