"""The Docker image and the wheel are built with `pip install .`: only the files named in pyproject's package-data are
inside. A stylesheet or script that is not listed works from the source tree (and in every test) and is missing in the
real installation, where the portal would show unstyled pages."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_every_static_file_and_template_is_packaged():
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["app"]
    packaged = {p for pat in patterns for p in (ROOT / "app").glob(pat) if p.is_file()}
    wanted = {p for folder in ("admin/static", "admin/templates") for p in (ROOT / "app" / folder).rglob("*")
              if p.is_file() and "__pycache__" not in p.parts}
    assert wanted - packaged == set(), f"not in package-data: {sorted(str(p.relative_to(ROOT)) for p in wanted - packaged)}"
