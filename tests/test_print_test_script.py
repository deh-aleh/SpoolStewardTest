"""scripts/print-test.sh: usage and argument checks (nothing is sent, no network needed)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "print-test.sh"
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"), reason="needs sh")


def run(*args, **env):
    e = {k: v for k, v in os.environ.items() if k not in ("GATEWAY", "PRINT_KEY")}
    e.update(env)
    return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True, env=e, timeout=30, stdin=subprocess.DEVNULL)


def test_no_arguments_prints_usage_and_exits_2():
    r = run()
    assert r.returncode == 2
    assert "SERVER[:PORT] QUEUE [USERNAME]" in r.stdout
    assert "GATEWAY" in r.stdout


def test_server_without_queue_is_a_usage_error():
    r = run("print.example.lan:8631")
    assert r.returncode == 2
    assert "SERVER[:PORT] QUEUE" in r.stdout


def test_gateway_variable_without_queue_is_a_usage_error():
    r = run(GATEWAY="print.example.lan:8631")
    assert r.returncode == 2
    assert "QUEUE [USERNAME]" in r.stdout


def test_no_machine_specific_defaults_left():
    text = SCRIPT.read_text()
    for old in ("HP7305", "print.lan", "Prova", "Crea una chiave"):
        assert old not in text


@pytest.mark.skipif(not shutil.which("ipptool"), reason="needs ipptool")
def test_unreachable_server_fails_readably():
    r = run("127.0.0.1:9", "office", "mario", PRINT_KEY="x")
    assert r.returncode == 1
    assert "Print test failed" in r.stderr
