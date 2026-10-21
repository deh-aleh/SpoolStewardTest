"""scripts/update.sh against a local 'release server' (file:// URLs) and a fake docker."""

import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UPDATE = ROOT / "scripts" / "update.sh"
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("curl") or not shutil.which("sha256sum"),
                                reason="needs sh, curl and sha256sum")

BACKUP_STUB = '#!/bin/sh\necho "backup.sh $*" >> "$LOG"\nif [ "$1" = backup ]; then mkdir -p "$2"; echo data > "$2/spoolsteward-v1.0.0-20260101-000000.tar.gz"; fi\n'


def build(rel: Path, version: str, files: dict[str, str], *, extra_members=(), corrupt_sum=False):
    """A release package like scripts/make-release-package.sh makes; `files` = path -> content."""
    files = {**files, "VERSION": version + "\n"}
    files["FILES"] = "\n".join([*files, "FILES"]) + "\n"
    top = f"spoolsteward-{version}"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in {**files, **dict(extra_members)}.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size, info.mode = len(data), 0o755 if name.startswith("scripts/") else 0o644
            tf.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    d = rel / f"v{version}"
    d.mkdir(parents=True)
    (d / f"{top}.tar.gz").write_bytes(blob)
    digest = "0" * 64 if corrupt_sum else hashlib.sha256(blob).hexdigest()
    (d / "SHA256SUMS").write_text(f"{digest}  {top}.tar.gz\n")


@pytest.fixture
def site(tmp_path):
    project, rel, bin_ = tmp_path / "pm", tmp_path / "rel", tmp_path / "bin"
    for d in (project / "scripts", project / "config" / "tls", project / "data", bin_):
        d.mkdir(parents=True)
    shutil.copy(UPDATE, project / "scripts" / "update.sh")
    (project / "scripts" / "backup.sh").write_text(BACKUP_STUB)
    (project / "scripts" / "backup.sh").chmod(0o755)
    (project / "scripts" / "old-helper.sh").write_text("old helper\n")
    (project / "docker-compose.yml").write_text("# compose 1.0.0\n")
    (project / "FILES").write_text("docker-compose.yml\nscripts/update.sh\nscripts/backup.sh\nscripts/old-helper.sh\nVERSION\nFILES\n")
    (project / "VERSION").write_text("1.0.0\n")
    (project / ".env").write_text("SPOOLSTEWARD_REGISTRY=ghcr.io/x\nSPOOLSTEWARD_VERSION=1.0.0\nSPOOLSTEWARD_TIMEZONE=Europe/Rome\n")
    (project / "config" / "tls" / "privkey.pem").write_text("KEY")
    (project / "data" / "keep.txt").write_text("mine")
    # fake docker: logs every call; `ps` answers "healthy" only for the versions listed in HEALTHY
    (bin_ / "docker").write_text(
        '#!/bin/sh\necho "docker $*" >> "$LOG"\n'
        'if [ "$2" = ps ]; then v="$(sed -n "s/^SPOOLSTEWARD_VERSION=//p" .env)"; case " $HEALTHY " in *" $v "*) echo healthy;; *) echo unhealthy;; esac; fi\n'
        '[ "$FAIL_PULL" = 1 ] && [ "$2" = pull ] && exit 1\nexit 0\n')
    (bin_ / "docker").chmod(0o755)
    build(rel, "1.1.0", {"docker-compose.yml": "# compose 1.1.0\n", "scripts/update.sh": UPDATE.read_text(),
                         "scripts/backup.sh": BACKUP_STUB, "scripts/new-helper.sh": "new helper\n"})

    def run(*args, healthy="1.1.0", stdin="", env_extra=None, cwd=None):
        env = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp_path), "LOG": str(tmp_path / "log"), "HEALTHY": healthy,
               "SPOOLSTEWARD_RELEASES_URL": f"file://{rel}", "SPOOLSTEWARD_RELEASES_API": "file:///nonexistent", "SPOOLSTEWARD_HEALTH_TRIES": "2", "SPOOLSTEWARD_HEALTH_WAIT": "0", **(env_extra or {})}
        return subprocess.run(["sh", "scripts/update.sh", *args], cwd=cwd or project, env=env, input=stdin, capture_output=True, text=True)

    run.project, run.rel, run.tmp = project, rel, tmp_path
    run.log = lambda: (tmp_path / "log").read_text() if (tmp_path / "log").exists() else ""
    run.env = lambda: (project / ".env").read_text()
    return run


def test_update_replaces_the_package_files_and_never_touches_user_data(site):
    r = site("1.1.0")
    assert r.returncode == 0, r.stderr + r.stdout
    p = site.project
    assert (p / "docker-compose.yml").read_text() == "# compose 1.1.0\n" and (p / "VERSION").read_text() == "1.1.0\n"
    assert (p / "scripts" / "new-helper.sh").is_file() and not (p / "scripts" / "old-helper.sh").exists()   # obsolete file removed
    assert "SPOOLSTEWARD_VERSION=1.1.0" in site.env() and "SPOOLSTEWARD_TIMEZONE=Europe/Rome" in site.env()             # the user's values stay
    assert (p / "config" / "tls" / "privkey.pem").read_text() == "KEY" and (p / "data" / "keep.txt").read_text() == "mine"
    assert not (p / ".update-new").exists() and not list(p.rglob("*.pm-tmp"))
    log = site.log()
    assert "backup.sh backup data/backups/pre-update" in log
    assert log.index("docker compose pull") < log.index("docker compose up -d --remove-orphans")
    assert "OK: now on 1.1.0" in r.stdout


def test_a_package_that_does_not_match_its_checksum_changes_nothing(site):
    shutil.rmtree(site.rel / "v1.1.0")
    build(site.rel, "1.1.0", {"docker-compose.yml": "# evil\n"}, corrupt_sum=True)
    r = site("1.1.0")
    assert r.returncode == 1 and "sha256" in r.stderr
    assert (site.project / "docker-compose.yml").read_text() == "# compose 1.0.0\n" and "SPOOLSTEWARD_VERSION=1.0.0" in site.env()
    assert "docker compose pull" not in site.log()


def test_a_package_with_a_path_to_the_users_files_is_refused(site):
    shutil.rmtree(site.rel / "v1.1.0")
    build(site.rel, "1.1.0", {"docker-compose.yml": "# x\n", ".env": "SPOOLSTEWARD_VERSION=6.6.6\n"})
    r = site("1.1.0")
    assert r.returncode == 1 and "unsafe path" in r.stderr and "SPOOLSTEWARD_VERSION=1.0.0" in site.env()


def test_unhealthy_new_version_puts_the_old_files_and_version_back(site):
    r = site("1.1.0", healthy="1.0.0")
    assert r.returncode == 1 and "going back to 1.0.0" in r.stderr
    p = site.project
    assert (p / "docker-compose.yml").read_text() == "# compose 1.0.0\n" and "SPOOLSTEWARD_VERSION=1.0.0" in site.env()
    assert (p / "scripts" / "old-helper.sh").read_text() == "old helper\n" and not (p / "scripts" / "new-helper.sh").exists()
    assert "running again" in r.stderr and "backup.sh restore" not in site.log()


def test_when_the_old_version_does_not_start_either_the_backup_can_be_restored(site):
    r = site("1.1.0", healthy="", stdin="YES\n")
    assert r.returncode == 1 and "does not start either" in r.stderr
    assert "backup.sh restore data/backups/pre-update/spoolsteward-v1.0.0-20260101-000000.tar.gz --yes" in site.log()
    r = site("1.1.0", healthy="", stdin="no\n")                       # asked again (the first try left 1.0.0 in .env)
    assert "backup.sh restore" in r.stderr                            # the instructions are printed instead


def test_failed_image_download_goes_back(site):
    r = site("1.1.0", env_extra={"FAIL_PULL": "1"})
    assert r.returncode == 1 and "the old files and version 1.0.0 are back" in r.stderr
    assert (site.project / "docker-compose.yml").read_text() == "# compose 1.0.0\n" and "SPOOLSTEWARD_VERSION=1.0.0" in site.env()


def test_check_mode_git_clone_and_portal_mode(site):
    r = site("--check")                                               # asks the (fake) releases API: nothing there, so it says so
    assert r.returncode == 1 and "could not look up" in r.stderr
    (site.rel / "latest.json").write_text('{"tag_name": "v1.1.0", "name": "x"}')
    r = site("--check", env_extra={"SPOOLSTEWARD_RELEASES_API": f"file://{site.rel}/latest.json"})
    assert r.returncode == 0 and "update available" in r.stdout and "wanted: 1.1.0" in r.stdout

    (site.project / ".git").mkdir()
    r = site("1.1.0")
    assert r.returncode == 1 and "git clone" in r.stderr
    (site.project / ".git").rmdir()

    env = {"PATH": f"{site.tmp / 'bin'}:/usr/bin:/bin", "LOG": str(site.tmp / "log"), "HEALTHY": "1.1.0",
           "SPOOLSTEWARD_RELEASES_URL": f"file://{site.rel}", "SPOOLSTEWARD_RELEASES_API": "file:///nonexistent", "SPOOLSTEWARD_HEALTH_TRIES": "1", "SPOOLSTEWARD_HEALTH_WAIT": "0"}
    r = subprocess.run(["sh", "scripts/update.sh", "--from-portal", "1.1.0"], cwd=site.project, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "backup.sh" not in site.log()                               # the portal made the backup already


def test_the_real_release_package_is_complete_and_has_nothing_of_the_users(tmp_path):
    out = subprocess.run(["sh", str(ROOT / "scripts" / "make-release-package.sh"), "3.2.1", str(tmp_path)], cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    pkg = tmp_path / "spoolsteward-3.2.1.tar.gz"
    assert (tmp_path / "SHA256SUMS").read_text().split()[0] == hashlib.sha256(pkg.read_bytes()).hexdigest()
    with tarfile.open(pkg) as tf:
        names = tf.getnames()
        files = tf.extractfile("spoolsteward-3.2.1/FILES").read().decode().split()
        assert tf.extractfile("spoolsteward-3.2.1/VERSION").read().decode().strip() == "3.2.1"
    assert all(n.startswith("spoolsteward-3.2.1") and ".." not in n for n in names)
    for needed in ("docker-compose.yml", "docker/env.example", "scripts/update.sh", "scripts/docker-init.sh", "scripts/backup.sh",
                   "scripts/cert-sync.sh", "docker/stack/cups/cupsd.conf", "VERSION", "FILES"):
        assert needed in files, needed
    assert not any(f.startswith((".env", "config", "data", ".git")) or f.endswith(".env") for f in files)
    assert all(f"spoolsteward-3.2.1/{f}" in names for f in files)
    compose = (ROOT / "docker-compose.yml").read_text()
    for mounted in ("docker/stack/cups/cupsd.conf", "docker/radius/clients.conf", "docker/radius/authorize"):
        assert f"./{mounted}" in compose and mounted in files        # whatever the compose file mounts must come with it


def test_a_git_clone_is_refused_even_before_it_has_a_dot_env(site):
    (site.project / ".env").unlink()
    (site.project / ".git").mkdir()
    r = site("1.1.0")
    assert r.returncode == 1 and "git clone" in r.stderr and ".env not found" not in r.stderr
    assert "docker compose pull" not in site.log()


def test_after_restoring_an_older_backup_the_files_of_that_version_are_installed_again(site):
    """The backup brings back .env (version 1.1.0) while the files here are of 1.0.0: update.sh 1.1.0 must not say 'already on'."""
    site.project.joinpath(".env").write_text(site.env().replace("SPOOLSTEWARD_VERSION=1.0.0", "SPOOLSTEWARD_VERSION=1.1.0"))
    r = site("1.1.0")
    assert r.returncode == 0 and "OK: now on 1.1.0" in r.stdout
    assert (site.project / "docker-compose.yml").read_text() == "# compose 1.1.0\n" and (site.project / "VERSION").read_text() == "1.1.0\n"
    r = site("1.1.0")                                                  # now the files agree: nothing to do
    assert "already on 1.1.0" in r.stdout


def test_a_beta_can_be_installed_by_naming_it_and_the_wrong_spellings_are_refused(site):
    build(site.rel, "1.2.0-beta.1", {"docker-compose.yml": "# compose beta\n", "scripts/update.sh": UPDATE.read_text(), "scripts/backup.sh": BACKUP_STUB})
    r = site("1.2.0-beta.1", healthy="1.2.0-beta.1")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "SPOOLSTEWARD_VERSION=1.2.0-beta.1" in site.env() and (site.project / "VERSION").read_text() == "1.2.0-beta.1\n"
    before = site.log()
    for bad in ("1.2.0-beta", "1.2.0b1", "1.2.0-rc.1", "1.2.0-beta.1; id"):
        r = site(bad)
        assert r.returncode == 1 and "not a version number" in r.stderr, bad
    assert site.log() == before                                       # nothing was run for them


def test_the_release_package_builder_takes_a_beta_and_refuses_junk(tmp_path):
    ok = subprocess.run(["sh", str(ROOT / "scripts" / "make-release-package.sh"), "3.2.1-beta.2", str(tmp_path)], cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr and (tmp_path / "spoolsteward-3.2.1-beta.2.tar.gz").is_file()
    for bad in ("3.2.1b2", "3.2.1-beta", "3.2.1-x"):
        assert subprocess.run(["sh", str(ROOT / "scripts" / "make-release-package.sh"), bad, str(tmp_path)], cwd=ROOT,
                              capture_output=True).returncode == 2
