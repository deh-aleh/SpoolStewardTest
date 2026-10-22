"""scripts/resources.sh: the CPU and RAM of the containers and of Docker itself, with a fake `docker`."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "resources.sh"
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("awk") or not shutil.which("ps"), reason="needs sh, awk and ps")

STATS = """spoolsteward-cups-1 0.00% 10.2MiB / 3.7GiB
spoolsteward-gateway-1 0.25% 89MiB / 3.7GiB
spoolsteward-mdns-1 1.50% 70.5MiB / 3.7GiB
spoolsteward-ipp-usb-1 0.00% 2048KiB / 3.7GiB
spoolsteward-big-1 0.00% 1.5GiB / 3.7GiB
"""


def run(tmp_path, stats=STATS, ids="a\nb\n"):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, project / "scripts" / "resources.sh")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text(
        '#!/bin/sh\nif [ "$1 $2 $3" = "compose ps -q" ]; then printf "%s" "$IDS"; exit 0; fi\n'
        'if [ "$1" = stats ]; then printf "%s" "$STATS"; exit 0; fi\nexit 1\n')
    (bin_ / "docker").chmod(0o755)
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "STATS": stats, "IDS": ids, "HOME": str(tmp_path)}
    return subprocess.run(["sh", "scripts/resources.sh"], cwd=project, env=env, capture_output=True, text=True)


def test_each_container_is_listed_with_its_ram_in_mb_and_the_totals_add_up(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    lines = {ln.split()[0]: ln for ln in r.stdout.splitlines() if ln.startswith("  spoolsteward")}
    assert "RAM     10 MB" in lines["spoolsteward-cups-1"] and "CPU    0.0%" in lines["spoolsteward-cups-1"]
    assert "RAM     89 MB" in lines["spoolsteward-gateway-1"] and ("CPU    0.2%" in lines["spoolsteward-gateway-1"] or "CPU    0.3%" in lines["spoolsteward-gateway-1"])
    assert "RAM      2 MB" in lines["spoolsteward-ipp-usb-1"]                 # 2048 KiB
    assert "RAM   1536 MB" in lines["spoolsteward-big-1"]                     # 1.5 GiB
    total = next(ln for ln in r.stdout.splitlines() if ln.startswith("Containers (sum)"))
    assert "CPU    1.8%" in total or "CPU    1.7%" in total                       # 0.00 + 0.25 + 1.50
    assert "RAM   1708 MB" in total                                             # 10.2 + 89 + 70.5 + 2 + 1536 = 1707.7
    assert any(ln.startswith("Docker (daemon, containerd, shims)") for ln in r.stdout.splitlines())
    assert any(ln.startswith("Total") for ln in r.stdout.splitlines())
    assert "0 MB of RAM" not in r.stderr


def test_all_zero_ram_says_how_to_turn_the_memory_cgroup_on(tmp_path):
    r = run(tmp_path, stats="spoolsteward-cups-1 0.00% 0B / 0B\nspoolsteward-gateway-1 0.20% 0B / 0B\n")
    assert r.returncode == 0
    assert "RAM      0 MB" in r.stdout
    assert "cgroup_enable=memory cgroup_memory=1" in r.stderr and "/boot/firmware/cmdline.txt" in r.stderr


def test_without_containers_it_says_so_and_fails(tmp_path):
    r = run(tmp_path, ids="")
    assert r.returncode == 1 and "docker compose up -d" in r.stderr and r.stdout == ""
