"""Tell administrators when a newer release exists. It never installs anything.

Once a day (SPOOLSTEWARD_UPDATE_CHECK) it asks the releases API one question: "what is the newest release?". Nothing about this
server is sent. The answer is kept in memory (app.state.update_info) and shown as a note in the portal to administrators.
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

from app.backup.core import app_version
from app.config import settings

log = logging.getLogger("app.updates")

CHECK_EVERY = 24 * 3600
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)", re.ASCII)
# the version of a beta as the package metadata writes it (1.2.3b1) or as the tag does (1.2.3-beta.1)
_BETA = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:b|-beta\.)(\d+)", re.ASCII)


def parse(text: str) -> tuple[int, int, int] | None:
    """'v1.2.3' -> (1, 2, 3); anything else (pre-releases, junk) -> None, so it is never offered."""
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
    a, b = parse(latest), _order(installed)   # only a final release is ever offered; the installed one may be a beta
    return a is not None and b is not None and (*a, 1, 0) > b


async def fetch_latest(client: httpx.AsyncClient | None = None) -> dict | None:
    """{'version': '1.2.0', 'url': 'https://...'} or None when the answer is unusable. Network errors are raised."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
    try:
        r = await client.get(settings.update_api_url, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        data = r.json()
    finally:
        if own:
            await client.aclose()
    tag = str(data.get("tag_name", ""))
    if parse(tag) is None or data.get("prerelease") or data.get("draft"):
        return None
    url = str(data.get("html_url", ""))
    return {"version": tag.lstrip("v"), "url": url if url.startswith("https://") else ""}


async def check_once(app) -> None:
    info = await fetch_latest()
    installed = app_version()
    app.state.update_info = info if info and newer(info["version"], installed) else None


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
# Only a version number travels in the request; the images always come from the registry set in .env.

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
    if state not in STATES or (version and parse(version) is None):
        return {}
    return {"state": state, "version": version, "backup": os.path.basename(str(data.get("backup", ""))),
            "ts": str(data.get("ts", ""))[:32]}


def pending_request() -> bool:
    return (_dir() / "request.json").exists()


def eligible_backup(db, now: datetime):
    """The scheduled backup that makes an update acceptable: enabled and last run succeeded (not too old, no error)."""
    from sqlalchemy import select

    from app.backup.service import health
    from app.db.models import BackupTarget

    return next((t for t in db.scalars(select(BackupTarget).order_by(BackupTarget.id)) if health(t, now) == "ok"), None)


def write_request(version: str, who: str, backup: str) -> None:
    """Hand the update to the updater. `version` must be a plain x.y.z of the same major as the installed one."""
    installed, wanted = _order(app_version()), parse(version)   # an installed beta may update to its final release
    if wanted is None or installed is None or wanted[0] != installed[0]:
        raise ValueError("version not allowed from the portal")
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".request.tmp"
    tmp.write_text(json.dumps({"version": version, "by": who, "backup": backup,
                               "ts": datetime.now(UTC).isoformat(timespec="seconds")}))
    os.replace(tmp, d / "request.json")
