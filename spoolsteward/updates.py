"""Tell administrators when a newer release exists. It never installs anything.

Once a day (SPOOLSTEWARD_UPDATE_CHECK) it asks the releases API of the project (SPOOLSTEWARD_UPDATE_REPOSITORY) for the
list of releases. Nothing about this server is sent. The newest release that may be offered (betas only with
SPOOLSTEWARD_UPDATE_BETA) is kept in memory (app.state.update_info) and shown as a note in the portal to administrators,
with two warnings: it is a test release (beta), it has breaking changes.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from spoolsteward.backup.core import app_version
from spoolsteward.config import settings

log = logging.getLogger("spoolsteward.updates")

CHECK_EVERY = 24 * 3600
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)", re.ASCII)
# the version of a beta as the package metadata writes it (1.2.3b1) or as the tag does (1.2.3-beta.1)
_BETA = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:b|-beta\.)(\d+)", re.ASCII)
# a release version as the tags, .env and the shell scripts write it: 1.2.3 or 1.2.3-beta.1
_RELEASE = re.compile(r"\d{1,6}\.\d{1,6}\.\d{1,6}(?:-beta\.\d{1,6})?", re.ASCII)
# "Breaking changes" as a heading of the release notes: "## Breaking changes" (any level) or the text underlined by === / ---
_BREAKING_ATX = re.compile(r"^ {0,3}#{1,6}[ \t]+breaking changes[ \t]*:?(?:[ \t]+#+)?[ \t]*\r?$", re.I | re.M | re.ASCII)
_BREAKING_SETEXT = re.compile(r"^ {0,3}breaking changes[ \t]*:?[ \t]*\r?\n {0,3}(?:=+|-+)[ \t]*\r?$", re.I | re.M | re.ASCII)
PER_PAGE = 100
MAX_PAGES = 10                # 1000 releases: far more than this project will have
MAX_PAGE_BYTES = 20_000_000   # GitHub allows 125,000 characters of notes per release: 100 of them fit
MAX_NOTES = 200_000           # characters of the notes that are searched for the heading


def parse(text: str) -> tuple[int, int, int] | None:
    """'v1.2.3' -> (1, 2, 3); anything else (betas, junk) -> None."""
    m = _VERSION.fullmatch(text)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _order(text: str) -> tuple | None:
    """Sort key: a beta comes before the final version it leads to (1.2.3-beta.2 < 1.2.3 < 1.2.4-beta.1)."""
    final = parse(text)
    if final:
        return (*final, 1, 0)
    m = _BETA.fullmatch(text)
    return (int(m[1]), int(m[2]), int(m[3]), 0, int(m[4])) if m else None


def newer(latest: str, installed: str) -> bool:
    a, b = _order(latest), _order(installed)
    return a is not None and b is not None and a > b


def breaking_notes(body) -> bool:
    """True when the release notes have a heading "Breaking changes". The notes are only searched, never shown."""
    text = body[:MAX_NOTES] if isinstance(body, str) else ""
    return bool(_BREAKING_ATX.search(text) or _BREAKING_SETEXT.search(text))


def breaking_jump(installed: tuple, target: tuple) -> bool:
    """Another major version, or (0.x, where everything can still change) another minor version."""
    return target[0] != installed[0] or (target[0] == 0 and target[1] != installed[1])


def api_url() -> str:
    return f"https://api.github.com/repos/{settings.update_repository}/releases"


def release_page(tag: str) -> str:
    """Built here from the repository and a checked tag, never taken from the answer."""
    return f"https://github.com/{settings.update_repository}/releases/tag/{tag}"


def releases_from(data) -> list[dict]:
    """The usable releases of one page of the API: [{'tag', 'version', 'order', 'beta', 'breaking'}]. Drafts, odd tags
    and anything that is not a release object are skipped."""
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("draft") is not False:
            continue
        tag = item.get("tag_name")
        if not isinstance(tag, str) or not _RELEASE.fullmatch(tag.removeprefix("v")):   # what release.yml publishes
            continue
        version = tag.removeprefix("v")
        out.append({"tag": tag, "version": version, "order": _order(version),
                    "beta": "-beta." in version or item.get("prerelease") is not False,
                    "breaking": breaking_notes(item.get("body"))})
    return out


async def _page(client: httpx.AsyncClient, n: int):
    async with client.stream("GET", api_url(), params={"per_page": PER_PAGE, "page": n},
                             headers={"Accept": "application/vnd.github+json"}) as r:
        r.raise_for_status()
        body = bytearray()
        async for chunk in r.aiter_bytes():
            body += chunk
            if len(body) > MAX_PAGE_BYTES:
                raise ValueError(f"the answer of the releases API is larger than {MAX_PAGE_BYTES} bytes")
    return json.loads(body)


async def fetch_releases(client: httpx.AsyncClient | None = None) -> list[dict]:
    """Every published release of the project (see releases_from). Network errors and a broken answer are raised."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
    found = []
    try:
        for n in range(1, MAX_PAGES + 1):
            data = await _page(client, n)
            if not isinstance(data, list):
                raise ValueError("the releases API did not answer with a list")
            found += releases_from(data)
            if len(data) < PER_PAGE:
                break
    finally:
        if own:
            await client.aclose()
    return found


def pick(releases: list[dict], installed: str, beta: bool) -> dict | None:
    """The release to offer: the newest one after `installed` (betas only if `beta`), or None.
    {'version', 'url', 'beta', 'breaking', 'major'}: `breaking` when any release after the installed one up to this one
    says so in its notes, or the jump itself is one (see breaking_jump); `major` when it is another major version, which
    the portal does not install."""
    now = _order(installed)
    if now is None:
        return None   # unknown installed version (a build from source): say nothing
    offered = [r for r in releases if r["order"] > now and (beta or not r["beta"])]
    if not offered:
        return None
    best = max(offered, key=lambda r: r["order"])
    breaking = breaking_jump(now, best["order"]) or any(r["breaking"] for r in releases if now < r["order"] <= best["order"])
    return {"version": best["version"], "url": release_page(best["tag"]), "beta": best["beta"], "breaking": breaking,
            "major": best["order"][0] != now[0]}


async def check_once(app) -> None:
    app.state.update_info = pick(await fetch_releases(), app_version(), settings.update_beta)


async def run_checker(app) -> None:
    app.state.update_info = None
    if not settings.update_check:
        return
    await asyncio.sleep(120)
    while True:
        try:
            await check_once(app)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # no network is normal on some sites: log once a day, show nothing
            log.warning("update check failed: %s", e)
        await asyncio.sleep(CHECK_EVERY)


# ---- talking to the optional updater container -------------------------------------------------------------------
# The gateway never touches Docker. To update from the portal it writes a small request file; the "updater" container
# (started only with `docker compose --profile autoupdate up -d`) reads it, makes the update and writes a status file.
# Only a version number (x.y.z or x.y.z-beta.N) travels in the request; the images always come from the registry set in .env.

HEARTBEAT_MAX_AGE = 60  # seconds: the updater writes its heartbeat every ~10 s
STATES = ("idle", "running", "ok", "rolled_back", "failed")


def _dir() -> Path:
    return Path(settings.update_dir)


def updater_alive() -> bool:
    try:
        return time.time() - (_dir() / "heartbeat").stat().st_mtime < HEARTBEAT_MAX_AGE
    except OSError:
        return False


def read_status() -> dict:
    """What the updater last reported: {'state': ..., 'version': ...} with only known values, or {} if nothing valid."""
    try:
        data = json.loads((_dir() / "status.json").read_text())
    except (OSError, ValueError):
        return {}
    state, version = data.get("state"), str(data.get("version", ""))
    if state not in STATES or (version and not _RELEASE.fullmatch(version)):
        return {}
    return {"state": state, "version": version, "backup": os.path.basename(str(data.get("backup", ""))),
            "ts": str(data.get("ts", ""))[:32]}


def pending_request() -> bool:
    return (_dir() / "request.json").exists()


def eligible_backup(db, now: datetime):
    """The scheduled backup that makes an update acceptable: enabled and last run succeeded (not too old, no error)."""
    from sqlalchemy import select

    from spoolsteward.backup.service import health
    from spoolsteward.db.models import BackupTarget

    return next((t for t in db.scalars(select(BackupTarget).order_by(BackupTarget.id)) if health(t, now) == "ok"), None)


def write_request(version: str, who: str, backup: str) -> None:
    """Hand the update to the updater. `version` must be x.y.z or x.y.z-beta.N, of the same major as the installed one and NEWER than
    it: the portal never goes back (scripts/update.sh does, from a terminal; the updater refuses it too)."""
    installed, wanted = _order(app_version()), _order(version) if _RELEASE.fullmatch(version) else None
    if wanted is None or installed is None or wanted[0] != installed[0] or wanted <= installed:
        raise ValueError("version not allowed from the portal")
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".request.tmp"
    tmp.write_text(json.dumps({"version": version, "by": who, "backup": backup,
                               "ts": datetime.now(UTC).isoformat(timespec="seconds")}))
    os.replace(tmp, d / "request.json")
