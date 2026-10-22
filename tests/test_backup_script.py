"""scripts/backup.sh (host-side backup/restore) and the certificate step of scripts/docker-init.sh, with a fake docker."""

import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "ghcr.io/x/spoolsteward:1.2.3"
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("tar") or not shutil.which("python3"),
                                reason="needs sh, tar and python3")

FAKE_DOCKER = r"""#!/bin/sh
echo "docker $*" >> "$LOG"
case "$1 $2" in
"compose config") echo '{"name": "spoolsteward", "services": {"cups": {"image": "ghcr.io/x/spoolsteward:1.2.3"}}}' ;;
"image inspect") exit "${IMAGE_RC:-0}" ;;
"compose exec") cat > /dev/null; printf 'SQLITE-FROM-GATEWAY' ;;
"compose stop"|"compose up") ;;
"run --rm")
    case "$*" in
    *"cp /w/spoolsteward.db"*)
        for a in "$@"; do case "$a" in *:/w:ro) mkdir -p "$FAKE_TMP" && cp "${a%:/w:ro}/spoolsteward.db" "$FAKE_TMP/restored.db" ;; esac; done ;;
    *"tar -C /c -cf"*) mkdir -p "$FAKE_TMP/empty" && echo q > "$FAKE_TMP/empty/queue" && tar -C "$FAKE_TMP/empty" -cf - . ;;
    esac ;;
esac
exit 0
"""


class Site:
    def __init__(self, root, log, bin_):
        self.root, self.log, self.bin = root, log, bin_

    def __truediv__(self, other):
        return self.root / other


@pytest.fixture
def site(tmp_path, monkeypatch):
    project, bin_ = tmp_path / "proj", tmp_path / "bin"
    for d in (project / "scripts", project / "config" / "tls", project / "data" / "audit-archive", bin_):
        d.mkdir(parents=True)
    for name in ("backup.sh", "docker-init.sh"):
        shutil.copy(ROOT / "scripts" / name, project / "scripts" / name)
    (bin_ / "docker").write_text(FAKE_DOCKER)
    (bin_ / "docker").chmod(0o755)
    (project / ".env").write_text("SPOOLSTEWARD_REGISTRY=ghcr.io/x\nSPOOLSTEWARD_VERSION=1.2.3\n")
    (project / "config" / "tls" / "privkey.pem").write_text("KEY")
    (project / "VERSION").write_text("1.2.3\n")
    log = tmp_path / "log"
    log.write_text("")
    monkeypatch.setenv("PATH", f"{bin_}:/usr/bin:/bin")
    monkeypatch.setenv("LOG", str(log))
    monkeypatch.setenv("FAKE_TMP", str(tmp_path / "faketmp"))
    monkeypatch.delenv("IMAGE_RC", raising=False)
    monkeypatch.delenv("SPOOLSTEWARD_REGISTRY", raising=False)
    return Site(project, log, bin_)


def sh(project, script, *args):
    return subprocess.run(["sh", f"scripts/{script}", *args], cwd=project.root, capture_output=True, text=True, timeout=60)


def calls(project):
    return project.log.read_text().splitlines()


def make_archive(path, *, db=b"BACKUP-DB", dot_env=None, names=("config/", "cups-config.tar")):
    """An archive shaped like the one `backup` builds: tar -C work -czf out . (names start with ./)."""
    with tarfile.open(path, "w:gz") as tf:
        def add(name, data=None):
            info = tarfile.TarInfo("./" + name)
            if data is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        if db is not None:
            add("spoolsteward.db", db)
        for n in names:
            add(n, None if n.endswith("/") else b"x")
        if dot_env is not None:
            add("dot-env", dot_env.encode())
    return path


def run_line(project):
    return [c for c in calls(project) if c.startswith("docker run") and "chown" in c]


def snapshot(project):
    return {str(p.relative_to(project.root)): p.read_bytes() for p in sorted(project.root.rglob("*")) if p.is_file() and p != project.log}


def test_backup_and_restore_use_the_stack_image_as_root_never_alpine(site, tmp_path):
    r = sh(site, "backup.sh", "backup", str(tmp_path / "out"))
    assert r.returncode == 0, r.stderr
    archive = next((tmp_path / "out").glob("spoolsteward-v1.2.3-*.tar.gz"))
    assert sh(site, "backup.sh", "restore", str(archive), "--yes").returncode == 0
    runs = [c for c in calls(site) if c.startswith("docker run")]
    assert len(runs) == 3
    for c in runs:
        assert "--user 0:0" in c and f" {IMAGE} " in c + " "
    assert "alpine" not in site.log.read_text()


def test_restore_with_the_image_missing_stops_nothing_and_says_how_to_get_it(site, tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_RC", "1")
    archive = make_archive(tmp_path / "b.tar.gz")
    before = snapshot(site)
    r = sh(site, "backup.sh", "restore", str(archive), "--yes")
    assert r.returncode != 0 and "docker compose pull" in r.stderr and IMAGE in r.stderr
    assert not any("compose stop" in c for c in calls(site)) and snapshot(site) == before
    (site / ".env").write_text("SPOOLSTEWARD_REGISTRY=\n")            # built from source
    r = sh(site, "backup.sh", "restore", str(archive), "--yes")
    assert r.returncode != 0 and "up -d --build" in r.stderr


def test_restore_refuses_bad_files_before_touching_anything(site, tmp_path):
    corrupt = tmp_path / "corrupt.tar.gz"
    corrupt.write_bytes(b"this is not an archive")
    cases = [make_archive(tmp_path / "nodb.tar.gz", db=None), tmp_path / "missing.tar.gz", corrupt]
    before = snapshot(site)
    for f in cases:
        r = sh(site, "backup.sh", "restore", str(f), "--yes")
        assert r.returncode != 0 and r.stderr, f
        assert not any("compose stop" in c or c.startswith("docker run") for c in calls(site)), f
        assert snapshot(site) == before, f


@pytest.mark.parametrize("current,backup_env,expected", [
    ("", None, "1000:1000"),
    ("PUID=501\nPGID=20\n", None, "501:20"),
    ("PUID=abc\nPGID=20\n", None, "1000:20"),
    ("PUID=1;rm -rf /\nPGID=\n", None, "1000:1000"),
    ("PUID=501\nPGID=20\n", "PUID=7\nPGID=8\n", "7:8"),
    ("PUID=501\nPGID=20\n", "SOMETHING=1\n", "1000:1000"),
])
def test_restore_owner_comes_from_the_env_in_force_after_the_restore(site, tmp_path, current, backup_env, expected):
    (site / ".env").write_text("SPOOLSTEWARD_VERSION=1.2.3\n" + current)
    archive = make_archive(tmp_path / "b.tar.gz", dot_env=backup_env)
    assert sh(site, "backup.sh", "restore", str(archive), "--yes").returncode == 0
    (line,) = run_line(site)
    assert f"chown {expected} /d/spoolsteward.db" in line and "chown :" not in line


def test_restore_happy_path_order_and_content(site, tmp_path):
    archive = make_archive(tmp_path / "b.tar.gz")
    r = sh(site, "backup.sh", "restore", str(archive), "--yes")
    assert r.returncode == 0, r.stderr
    c = calls(site)
    stop = next(i for i, x in enumerate(c) if "compose stop gateway mdns" in x)
    runs = [i for i, x in enumerate(c) if x.startswith("docker run")]
    up = next(i for i, x in enumerate(c) if "compose up -d" in x)
    assert stop < runs[0] < runs[1] < up
    assert max(i for i, x in enumerate(c) if "image inspect" in x) < stop
    assert (tmp_path / "faketmp" / "restored.db").read_bytes() == b"BACKUP-DB"
    assert (site / "config").is_dir() and r.stdout.count("restored.") == 1


def test_docker_init_shows_the_openssl_error_and_stops(site):
    (site / "docker").mkdir()
    shutil.copy(ROOT / "docker" / "env.example", site / "docker" / "env.example")
    (site / "config" / "tls" / "privkey.pem").unlink()
    (site / ".env").unlink()
    (site.bin / "openssl").write_text('#!/bin/sh\nif [ "$1" = rand ]; then echo abcdefghijklmnopqrstuvwxyz0123456789abcdefghijklmnopqrstuvwxyz; exit 0; fi\n'
                                      'echo "boom: fake openssl failure" >&2; exit 3\n')
    (site.bin / "openssl").chmod(0o755)
    r = sh(site, "docker-init.sh", "print.lan")
    assert r.returncode == 1
    assert "boom: fake openssl failure" in r.stderr and "can not create the TLS certificate" in r.stderr


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_docker_init_long_name_gets_localhost_as_cn_and_stays_in_the_alt_names(site):
    (site / "config" / "tls" / "privkey.pem").unlink()
    long = "a" * 66 + ".example.com"
    assert len(long) > 64
    r = sh(site, "docker-init.sh", long)
    assert r.returncode == 0, r.stderr
    assert "longer than 64" in r.stdout
    cert = site / "config" / "tls" / "fullchain.pem"
    text = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-subject", "-ext", "subjectAltName"],
                          capture_output=True, text=True).stdout
    assert "CN" in text and "localhost" in text.splitlines()[0] and long in text
