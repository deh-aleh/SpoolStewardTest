"""Nothing that belongs to the maintainer alone may be tracked by git: local notes, tool folders, secrets, database files,
and the maintainer's own paths or address. The files are ignored (.gitignore), this fails if one is added anyway (git add -f)."""

import fnmatch
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_FILES = ("NOTE.md", "TEST.md", "CLAUDE.md", ".claude/*", "graphify-out/*", ".env", ".env.*", "*.odt", "*.db", "*.db-wal",
                 "*.db-shm", "config/*", "data/*", "certs/*", "*.key", "privkey.pem")
EXAMPLE_FILES = ("docker/env.example", "deploy/env.example")    # templates with no secrets: allowed even though they start with ".env"
# written in pieces so that this file does not contain what it looks for
PRIVATE_TEXT = ("/home/" + "vaghar", "/mnt/" + "sharedPartition", "vag" + "har", "proton" + ".me")


def _tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [p for p in out.decode().split("\0") if p]


def test_no_private_file_is_tracked():
    bad = [p for p in _tracked() if p not in EXAMPLE_FILES and any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(Path(p).name, g) for g in PRIVATE_FILES)]
    assert not bad, f"private files in git (git rm --cached): {bad}"


def test_no_private_text_in_tracked_files():
    found = []
    for p in _tracked():
        if p == "tests/test_repo_hygiene.py" or not (ROOT / p).is_file():
            continue
        try:
            text = (ROOT / p).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue    # binary file
        found += [f"{p}: {w}" for w in PRIVATE_TEXT if w in text]
    assert not found, f"maintainer-only text in tracked files: {found}"
