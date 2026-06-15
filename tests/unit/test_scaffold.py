from __future__ import annotations

import stat
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


def _read_env(directory: Path) -> dict[str, str]:
    env = {}
    for line in (directory / ".env").read_text().splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def test_scaffold_writes_files_and_dirs(tmp_path):
    target = tmp_path / "deploy"
    result = scaffold.scaffold(target, admin_password="pw")
    for rel in scaffold.SCAFFOLD_FILES:
        assert (target / rel).is_file(), rel
    for d in scaffold.SCAFFOLD_DIRS:
        assert (target / d).is_dir(), d
    assert (target / ".env").is_file()
    mode = (target / "install-cron.sh").stat().st_mode
    assert mode & stat.S_IXUSR
    # the executable in a subdirectory gets the bit too
    assert (target / "nginx" / "entrypoint.sh").stat().st_mode & stat.S_IXUSR
    assert result.directory == target


def test_scaffold_env_is_private(tmp_path):
    target = tmp_path / "a"
    scaffold.scaffold(target, admin_password="pw")
    assert stat.S_IMODE((target / ".env").stat().st_mode) == 0o600


def test_scaffold_tls_key_is_private(tmp_path):
    cert = tmp_path / "c.pem"
    cert.write_text("CERT")
    key = tmp_path / "k.pem"
    key.write_text("KEY")
    target = tmp_path / "a"
    scaffold.scaffold(target, admin_password="pw", tls_cert=cert, tls_key=key)
    assert stat.S_IMODE((target / "certs" / "privkey.pem").stat().st_mode) == 0o600


def test_scaffold_rejects_invalid_cert_cn(tmp_path):
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.scaffold(tmp_path / "a", admin_password="pw", cert_cn="bad\nhost")


def test_scaffold_generates_real_secrets(tmp_path):
    scaffold.scaffold(tmp_path / "a", admin_password="pw")
    env = _read_env(tmp_path / "a")
    assert len(env["COMPENDIUM_JWT_SECRET_KEY"]) >= 32
    assert env["COMPENDIUM_SECRET_KEY"]
    assert env["POSTGRES_PASSWORD"] and env["POSTGRES_PASSWORD"] != "change-me-postgres"
    assert env["COMPENDIUM_ADMIN_USERNAME"] == "admin"
    assert env["COMPENDIUM_ADMIN_PASSWORD"] == "pw"
    assert "change-me" not in (tmp_path / "a" / ".env").read_text()


def test_scaffold_secrets_differ_between_runs(tmp_path):
    scaffold.scaffold(tmp_path / "a", admin_password="pw")
    scaffold.scaffold(tmp_path / "b", admin_password="pw")
    assert _read_env(tmp_path / "a")["COMPENDIUM_JWT_SECRET_KEY"] != \
        _read_env(tmp_path / "b")["COMPENDIUM_JWT_SECRET_KEY"]


def test_scaffold_generates_admin_password_when_omitted(tmp_path):
    result = scaffold.scaffold(tmp_path / "a")
    assert result.admin_password_generated is True
    assert _read_env(tmp_path / "a")["COMPENDIUM_ADMIN_PASSWORD"] == result.admin_password
    assert result.admin_password


def test_scaffold_no_secret_key(tmp_path):
    scaffold.scaffold(tmp_path / "a", admin_password="pw", with_secret_key=False)
    assert "COMPENDIUM_SECRET_KEY" not in _read_env(tmp_path / "a")
    assert "# COMPENDIUM_SECRET_KEY=" in (tmp_path / "a" / ".env").read_text()


def test_scaffold_sets_image_and_hostname(tmp_path):
    scaffold.scaffold(
        tmp_path / "a", admin_password="pw",
        image="ghcr.io/statyk/compendium:1.3.0", cert_cn="library.example.org",
    )
    env = _read_env(tmp_path / "a")
    assert env["COMPENDIUM_IMAGE"] == "ghcr.io/statyk/compendium:1.3.0"
    assert env["COMPENDIUM_CERT_CN"] == "library.example.org"
    assert env["COMPENDIUM_ALLOWED_HOSTS"] == "library.example.org"
    assert env["COMPENDIUM_PUBLIC_BASE_URL"] == "https://library.example.org"


def test_scaffold_default_cert_cn_no_allowed_hosts(tmp_path):
    scaffold.scaffold(tmp_path / "a", admin_password="pw")
    assert "COMPENDIUM_ALLOWED_HOSTS" not in _read_env(tmp_path / "a")


def test_scaffold_refuses_existing_without_force(tmp_path):
    target = tmp_path / "a"
    scaffold.scaffold(target, admin_password="pw")
    (target / "docker-compose.yml").write_text("EDITED")
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.scaffold(target, admin_password="pw")
    assert (target / "docker-compose.yml").read_text() == "EDITED"


def test_scaffold_force_overwrites(tmp_path):
    target = tmp_path / "a"
    scaffold.scaffold(target, admin_password="pw")
    (target / "docker-compose.yml").write_text("EDITED")
    scaffold.scaffold(target, admin_password="pw", force=True)
    assert (target / "docker-compose.yml").read_text() != "EDITED"


def test_scaffold_tls_pair_copied(tmp_path):
    cert = tmp_path / "c.pem"
    cert.write_text("CERT")
    key = tmp_path / "k.pem"
    key.write_text("KEY")
    target = tmp_path / "a"
    result = scaffold.scaffold(target, admin_password="pw", tls_cert=cert, tls_key=key)
    assert (target / "certs" / "fullchain.pem").read_text() == "CERT"
    assert (target / "certs" / "privkey.pem").read_text() == "KEY"
    assert result.using_supplied_cert is True


def test_scaffold_tls_requires_both(tmp_path):
    cert = tmp_path / "c.pem"
    cert.write_text("CERT")
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.scaffold(tmp_path / "a", admin_password="pw", tls_cert=cert)


def test_scaffold_tls_missing_path_errors(tmp_path):
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.scaffold(
            tmp_path / "a", admin_password="pw",
            tls_cert=tmp_path / "nope.pem", tls_key=tmp_path / "nope2.pem",
        )
