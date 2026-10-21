"""docker/env.example is the only template of .env: its values must be the program's defaults, and docker-init.sh must fill it."""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from spoolsteward.config import Settings

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "docker" / "env.example"


# part A of the template: read by docker compose and scripts/update.sh, not by the program (same prefix, so named here)
COMPOSE_ONLY = {"SPOOLSTEWARD_REGISTRY", "SPOOLSTEWARD_VERSION"}


def active_options():
    for line in TEMPLATE.read_text().splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            yield m.group(1), m.group(2)


def test_every_active_spoolsteward_value_is_the_programs_own_default():
    """A user who copies an option out of the template must not change how the gateway behaves by doing so."""
    defaults, checked = Settings(_env_file=None), 0
    for key, raw in active_options():
        if not key.startswith("SPOOLSTEWARD_") or raw == "__GENERATED__" or key in COMPOSE_ONLY:
            continue
        field = key[len("SPOOLSTEWARD_"):].lower()
        if field == "timezone":
            continue                                       # the user is told to set it
        assert hasattr(defaults, field), f"{key} is in docker/env.example but is not a setting"
        assert getattr(Settings(_env_file=None, **{field: raw}), field) == getattr(defaults, field), \
            f"{key}={raw!r} in docker/env.example differs from the default in spoolsteward/config.py"
        checked += 1
    assert checked > 30


def test_the_old_gateway_env_is_gone_from_the_template():
    text = TEMPLATE.read_text()
    assert "config/gateway.env" not in text and "env-sync" not in text and "PostgreSQL" not in text


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_docker_init_makes_a_private_env_with_a_secret_and_never_overwrites_it(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docker").mkdir()
    shutil.copy(ROOT / "scripts" / "docker-init.sh", tmp_path / "scripts")
    shutil.copy(TEMPLATE, tmp_path / "docker" / "env.example")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    run = lambda: subprocess.run(["sh", "scripts/docker-init.sh", "10.0.0.5", "print.lan"], cwd=tmp_path,  # noqa: E731
                                 capture_output=True, text=True)
    r = run()
    assert r.returncode == 0, r.stderr
    env = (tmp_path / ".env").read_text()
    assert "__" not in env
    assert f"PUID={os.getuid()}" in env and f"PGID={os.getgid()}" in env and "SPOOLSTEWARD_VERSION=1.2.3" in env
    assert f"PROJECT_DIR={tmp_path}" in env
    key = re.search(r"^SPOOLSTEWARD_SECRET_KEY=(.+)$", env, re.M).group(1)
    assert len(key) == 64 and re.fullmatch(r"[A-Za-z0-9_-]+", key)                   # the minimum the gateway accepts
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    assert (tmp_path / "config" / "tls" / "fullchain.pem").is_file() and (tmp_path / "data" / "backups").is_dir()
    (tmp_path / ".env").write_text("PUID=7\nSPOOLSTEWARD_SECRET_KEY=mine\n")
    assert run().returncode == 0
    assert (tmp_path / ".env").read_text() == "PUID=7\nSPOOLSTEWARD_SECRET_KEY=mine\n"   # an existing file is never rewritten


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_docker_init_never_modifies_an_existing_env_and_warns_about_a_missing_or_weak_key(tmp_path):
    """A .env from an older setup or written by hand stays byte for byte as it is; a key problem is only reported."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docker").mkdir()
    shutil.copy(ROOT / "scripts" / "docker-init.sh", tmp_path / "scripts")
    shutil.copy(TEMPLATE, tmp_path / "docker" / "env.example")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    env = tmp_path / ".env"
    run = lambda: subprocess.run(["sh", "scripts/docker-init.sh"], cwd=tmp_path, capture_output=True, text=True)  # noqa: E731

    def kept(content: bytes) -> subprocess.CompletedProcess:
        env.write_bytes(content)
        r = run()
        assert r.returncode == 0 and "kept the existing .env" in r.stdout
        assert env.read_bytes() == content                                          # never touched, not even a newline
        return r

    r = kept(b"PUID=7\n# SPOOLSTEWARD_SECRET_KEY=commented-out\nPGID=8")              # no line, no newline at the end
    assert "WARNING: .env has no SPOOLSTEWARD_SECRET_KEY line" in r.stderr
    assert "openssl rand -hex 32" in r.stderr and "docs/en/troubleshooting.md" in r.stderr
    for value in (b"none", b"__GENERATED__", b"short-key-" + b"s" * 50):
        r = kept(b"PUID=7\nSPOOLSTEWARD_SECRET_KEY=" + value + b"\n")
        assert "WARNING: SPOOLSTEWARD_SECRET_KEY" in r.stderr
    assert "s" * 50 not in r.stderr + r.stdout                                      # a real key is never printed
    r = kept(b"SPOOLSTEWARD_SECRET_KEY=" + b"0123456789abcdef" * 4 + b"\n")       # what openssl rand -hex 32 prints
    assert "WARNING" not in r.stderr


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_docker_init_takes_the_version_from_pyproject_in_a_clone(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docker").mkdir()
    shutil.copy(ROOT / "scripts" / "docker-init.sh", tmp_path / "scripts")
    shutil.copy(TEMPLATE, tmp_path / "docker" / "env.example")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.4.2"\n')
    assert subprocess.run(["sh", "scripts/docker-init.sh"], cwd=tmp_path, capture_output=True).returncode == 0
    assert "SPOOLSTEWARD_VERSION=0.4.2" in (tmp_path / ".env").read_text()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_docker_init_writes_a_beta_the_way_the_images_are_tagged(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docker").mkdir()
    shutil.copy(ROOT / "scripts" / "docker-init.sh", tmp_path / "scripts")
    shutil.copy(TEMPLATE, tmp_path / "docker" / "env.example")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.4.2b3"\n')
    assert subprocess.run(["sh", "scripts/docker-init.sh"], cwd=tmp_path, capture_output=True).returncode == 0
    assert "SPOOLSTEWARD_VERSION=0.4.2-beta.3\n" in (tmp_path / ".env").read_text()
