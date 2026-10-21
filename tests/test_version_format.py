"""The version rule "X.Y.Z or X.Y.Z-beta.N" is written three times: app/updates.py, the valid() of scripts/update.sh and
the valid() of the updater container. A version ends up in .env, in image names and in a URL, so the three must agree.
Every case is applied to all three; where one disagrees the pair is listed in KNOWN_GAPS (xfail, strict) instead of
being hidden: the day it is fixed the test turns red and the entry must be removed."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import updates

ROOT = Path(__file__).resolve().parent.parent
SHELLS = {"update.sh": ROOT / "scripts" / "update.sh", "updater.sh": ROOT / "docker" / "stack" / "updater" / "updater.sh"}
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh") or not shutil.which("grep"),
                                reason="needs sh and grep")


def shell_valid(script: Path, text: str) -> bool:
    """Runs the script's own valid() line, extracted from the source, not a copy of its regex."""
    line = next(ln for ln in script.read_text().splitlines() if ln.startswith("valid()"))
    return subprocess.run(["sh", "-c", line + '\nvalid "$1"', "sh", text], capture_output=True).returncode == 0


def python_valid(text: str) -> bool:
    return updates._order(text) is not None          # finals and betas, as the checker and write_request see them


IMPLEMENTATIONS = {"updates.py": python_valid, **{n: (lambda t, p=p: shell_valid(p, t)) for n, p in SHELLS.items()}}

VALID = ["1.2.3", "1.2.3-beta.4", "10.20.30"]
INVALID = ["1.2", "1.2.3-rc.1", "1.2.3-beta", "1.2.3-beta.", "1.2.3-beta.4-x", "1.2.3-BETA.1", "1.2.3-beta.1.2", "",
           "1.2.3; rm -rf /", "$(id).2.3", "../1.2.3", "1.2.3/../x"]
# all three accept these today, which is lenient but harmless (the number is only compared and printed)
AGREED_LENIENT = ["01.2.3"]
# (implementation, input) pairs that SHOULD be refused and are not
KNOWN_GAPS = {
    ("updates.py", "v1.2.3"),            # by design: release tags carry the v and fetch_latest() strips it; the shells refuse it
    ("updates.py", "1.2.3b1"),           # by design: the spelling of package metadata for a beta
}
SHOULD_BE_REFUSED = ["v1.2.3", " 1.2.3", "1.2.3 ", "1.2.3\n", "1.2.3b1", "١.٢.٣", "1.2.3\nfoo", "foo\n1.2.3"]


def case(impl, text, expected):
    marks = [pytest.mark.xfail(strict=True, reason="known gap, see KNOWN_GAPS")] if (impl, text) in KNOWN_GAPS else []
    return pytest.param(impl, text, expected, marks=marks, id=f"{impl}-{text!r}")


CASES = ([case(i, t, True) for i in IMPLEMENTATIONS for t in VALID + AGREED_LENIENT]
         + [case(i, t, False) for i in IMPLEMENTATIONS for t in INVALID + SHOULD_BE_REFUSED])


@pytest.mark.parametrize("impl,text,expected", CASES)
def test_version_rule(impl, text, expected):
    assert IMPLEMENTATIONS[impl](text) is expected


def test_the_shell_validators_are_still_the_same_text():
    lines = {n: next(ln for ln in p.read_text().splitlines() if ln.startswith("valid()")) for n, p in SHELLS.items()}
    assert len(set(lines.values())) == 1, lines
    assert re.search(r"\(-beta\\\.\[0-9\]\+\)\?\$'", lines["update.sh"])
