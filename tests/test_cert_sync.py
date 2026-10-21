"""scripts/cert-sync.sh with a fake rsync (copies from a local folder) and a fake docker (records the call)."""

import json
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cert-sync.sh"
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("openssl"), reason="needs sh and openssl")


def write_pair(folder: Path, days_left=60, wrong_key=False):
    folder.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - timedelta(days=100)).not_valid_after(now + timedelta(days=days_left))
            .sign(key, hashes.SHA256()))
    (folder / "fullchain.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    other = ec.generate_private_key(ec.SECP256R1()) if wrong_key else key
    (folder / "privkey.pem").write_bytes(other.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


@pytest.fixture
def site(tmp_path):
    project, remote, bin_ = tmp_path / "pm", tmp_path / "remote", tmp_path / "bin"
    (project / "scripts").mkdir(parents=True)
    (project / "config" / "tls").mkdir(parents=True)
    bin_.mkdir()
    shutil.copy(SCRIPT, project / "scripts" / "cert-sync.sh")
    (tmp_path / "id").write_text("key")
    (project / ".env").write_text(f"CERT_SYNC_HOST=npm.lan\nCERT_SYNC_USER=certs\nCERT_SYNC_KEY={tmp_path / 'id'}\n"
                                  f"CERT_SYNC_REMOTE_DIR=/live/npm-3/\n")
    # fake rsync: the last two arguments are  user@host:/dir/file  and the local target
    (bin_ / "rsync").write_text(f'#!/bin/sh\nfor a; do last="$a"; src="$prev"; prev="$a"; done\n'
                                f'f="${{src##*/}}"; [ -e "{remote}/$f" ] || {{ echo "no such file" >&2; exit 23; }}\n'
                                f'cp "{remote}/$f" "$last"\n')
    (bin_ / "docker").write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path}/docker.log"\n')
    (bin_ / "ssh").write_text("#!/bin/sh\nexit 0\n")
    for f in bin_.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IXUSR)

    def run(*args):
        env = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp_path)}
        return subprocess.run(["sh", str(project / "scripts" / "cert-sync.sh"), *args], cwd=project, env=env,
                              capture_output=True, text=True)

    run.project, run.remote, run.log = project, remote, tmp_path / "docker.log"
    run.status = lambda: json.loads((project / "config" / "tls" / "sync-status.json").read_text())
    return run


def test_installs_a_new_certificate_and_restarts_once(site):
    write_pair(site.remote)
    r = site()
    assert r.returncode == 0, r.stderr
    tls = site.project / "config" / "tls"
    assert (tls / "fullchain.pem").read_bytes() == (site.remote / "fullchain.pem").read_bytes()
    assert stat.S_IMODE((tls / "privkey.pem").stat().st_mode) == 0o600
    assert site.log.read_text().strip() == "compose restart gateway"
    assert site.status()["ok"] is True and site.status()["changed"] is True

    r = site()                                             # the same pair again: nothing to do, no restart
    assert r.returncode == 0 and "up to date" in r.stdout
    assert site.log.read_text().count("restart") == 1 and site.status()["changed"] is False


def test_keeps_the_old_pair_aside(site):
    write_pair(site.project / "config" / "tls")
    old = (site.project / "config" / "tls" / "fullchain.pem").read_bytes()
    write_pair(site.remote)
    assert site().returncode == 0
    assert (site.project / "config" / "tls" / "previous" / "fullchain.pem").read_bytes() == old


def test_a_key_that_does_not_match_is_refused_and_reported(site):
    write_pair(site.project / "config" / "tls")
    before = (site.project / "config" / "tls" / "privkey.pem").read_bytes()
    write_pair(site.remote, wrong_key=True)
    r = site()
    assert r.returncode == 1 and "does not belong" in r.stderr
    assert (site.project / "config" / "tls" / "privkey.pem").read_bytes() == before     # nothing was touched
    assert not site.log.exists()
    assert site.status()["ok"] is False and "does not belong" in site.status()["message"]


def test_an_expired_certificate_is_refused(site):
    write_pair(site.remote, days_left=-1)
    r = site()
    assert r.returncode == 1 and "expired" in r.stderr and not site.log.exists()


def test_download_error_and_missing_settings_are_reported(site):
    r = site()                                              # nothing on the remote side
    assert r.returncode == 1 and site.status()["ok"] is False
    (site.project / ".env").write_text("CERT_SYNC_HOST=x\n")
    r = site()
    assert r.returncode == 1 and "CERT_SYNC_USER" in r.stderr


def test_dry_run_changes_nothing(site):
    write_pair(site.remote)
    r = site("--dry-run")
    assert r.returncode == 0 and "nothing was changed" in r.stdout
    assert not (site.project / "config" / "tls" / "fullchain.pem").exists() and not site.log.exists()
    assert not (site.project / "config" / "tls" / "sync-status.json").exists()
