"""The start script of the cups container: cupsd is its child, and the container lives exactly as long as cupsd does.

cupsd and the CUPS tools are replaced by small fakes; the script is the real one."""

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "docker" / "stack" / "cups" / "entrypoint.sh"


@pytest.fixture
def site(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "cupsd").write_text(
        '#!/bin/sh\necho "$$" > "$FAKE_DIR/cupsd.pid"\necho "$*" > "$FAKE_DIR/cupsd.args"\ntrap "exit 0" TERM\n'
        'while :; do sleep 0.1; done\n')
    for tool in ("lpstat", "lpadmin"):
        (bin_ / tool).write_text("#!/bin/sh\nexit 0\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    (tmp_path / "error_log").write_text("")
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "FAKE_DIR": str(tmp_path), "CUPS_QUEUES": "",
           "SPOOLSTEWARD_QUEUES_FILE": str(tmp_path / "queues"), "CUPS_ERROR_LOG": str(tmp_path / "error_log")}
    procs = []

    def start():
        out = open(tmp_path / "out.txt", "w")
        p = subprocess.Popen(["sh", str(SCRIPT)], env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        procs.append(p)
        deadline = time.time() + 10
        while not (tmp_path / "cupsd.pid").exists() and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)           # the script goes on to its queues and waits
        return p

    yield type("Site", (), {"start": staticmethod(start), "dir": tmp_path})
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGKILL)   # the tail it leaves behind
        except ProcessLookupError:
            pass


def test_cupsd_is_started_in_the_foreground_as_a_child(site):
    p = site.start()
    assert (site.dir / "cupsd.args").read_text().strip() == "-f"
    assert p.poll() is None                      # the script is still there, waiting for cupsd


def test_the_container_ends_with_an_error_when_cupsd_dies(site):
    """Docker restarts a container that exits (restart: unless-stopped); it never restarts one that is merely unhealthy."""
    p = site.start()
    os.kill(int((site.dir / "cupsd.pid").read_text()), signal.SIGKILL)
    code = p.wait(timeout=10)
    assert code != 0
    assert "cupsd stopped" in (site.dir / "out.txt").read_text()


def test_even_a_clean_exit_of_cupsd_ends_the_container_with_an_error(site):
    p = site.start()
    os.kill(int((site.dir / "cupsd.pid").read_text()), signal.SIGTERM)    # the fake exits with 0
    assert p.wait(timeout=10) == 1


def test_docker_stop_stops_cupsd_first_and_ends_cleanly(site):
    p = site.start()
    cupsd = int((site.dir / "cupsd.pid").read_text())
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=10) == 0
    with pytest.raises(ProcessLookupError):
        os.kill(cupsd, 0)                          # cupsd got the TERM and is gone
