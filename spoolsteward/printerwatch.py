"""Printer problems (paper jam, out of paper, out of ink or toner) shown as notices.

Once a minute the queues are read from CUPS (state reasons and ink levels, the same data as the Printers page).
A problem must be seen twice in a row before it is reported, so a printer that flickers does not fill the bell.

    administrators   a notice per printer and problem (kind "printer_problem"; ink that is only running low is for them alone)
    users            whoever has a print waiting on that printer gets a notice in their own bell, once per problem
                     (SPOOLSTEWARD_PRINTER_ALERTS_USERS=false turns this off)

A notice closes by itself when the problem is gone.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from spoolsteward import notices
from spoolsteward.config import settings
from spoolsteward.db.models import JobKind, JobLog, JobState
from spoolsteward.policy import ordering

log = logging.getLogger("spoolsteward.printerwatch")
KIND = "printer_problem"
PROBLEMS = ("jam", "paper", "ink", "ink_low")
USER_PROBLEMS = ("jam", "paper", "ink")           # "running low" is not worth bothering a user who is printing
CHECK_EVERY = 60
CONFIRM = 2                                       # consecutive checks that show the same problem before it is reported
WAITING_SINCE = timedelta(days=3)                 # a print older than this is not "waiting": the reconcile step gave up on it

_REASON = {"media-jam": "jam", "media-empty": "paper", "media-needed": "paper",
           "marker-supply-empty": "ink", "toner-empty": "ink", "ink-empty": "ink",
           "marker-supply-low": "ink_low", "toner-low": "ink_low", "ink-low": "ink_low"}
_streak: dict[tuple[str, str], int] = {}          # (printer, problem) -> consecutive checks that showed it


def problems_of(queue: dict) -> dict[str, str]:
    """{problem: short description} for one queue as read by spoolsteward.admin.printers._cups_queues."""
    found: dict[str, str] = {}
    for reason in queue.get("reasons", []):
        base = reason.rsplit("-", 1)[0] if reason.endswith(("-error", "-warning", "-report")) else reason
        if base in _REASON:
            found.setdefault(_REASON[base], reason)
    empty = [i["name"] for i in queue.get("ink", []) if i.get("level") == 0]
    low = [i["name"] for i in queue.get("ink", []) if i.get("low") and i.get("level") != 0]
    if empty:
        found["ink"] = ", ".join(empty)
    if low and "ink" not in found:
        found["ink_low"] = ", ".join(low)
    if "ink" in found and "ink_low" in found:
        del found["ink_low"]
    return found


def evaluate(queues: list[dict]) -> dict[str, dict[str, str]]:
    """{queue name: {problem: description}} for the physical queues that have a problem."""
    out = {}
    for q in queues:
        if ordering.is_virtual(q.get("device_uri", "")):
            continue
        p = problems_of(q)
        if p:
            out[q["name"]] = {"label": q.get("info") or q["name"], "problems": p}
    return out


def _waiting_users(sm, printer: str, now: datetime) -> list[int]:
    with sm() as db:
        return sorted(set(db.scalars(select(JobLog.user_id).where(
            JobLog.printer == printer, JobLog.kind == JobKind.print, JobLog.state.in_((JobState.accepted, JobState.held)),
            JobLog.user_id.is_not(None), JobLog.ts >= now - WAITING_SINCE))))


def apply(sm, found: dict[str, dict], names: list[str], now: datetime | None = None) -> None:
    """Report what `found`, close what is gone. `names`: every physical queue that was looked at."""
    now = now or datetime.now(UTC)
    for name in names:
        info = found.get(name, {"label": name, "problems": {}})
        for problem in PROBLEMS:
            key = f"{name}:{problem}"
            if problem not in info["problems"]:
                _streak.pop((name, problem), None)
                notices.resolve(sm, KIND, key)
                notices.resolve_users(sm, KIND, key)
                continue
            seen = _streak[(name, problem)] = _streak.get((name, problem), 0) + 1
            if seen < CONFIRM:
                continue
            detail = {"printer": info["label"], "problem": problem, "what": info["problems"][problem]}
            notices.report(sm, KIND, key, detail)
            if settings.printer_alerts_users and problem in USER_PROBLEMS:
                for uid in _waiting_users(sm, name, now):
                    notices.notify_user_once(sm, uid, KIND, key, detail)


async def check_once(app) -> int:
    from spoolsteward.admin.printers import _cups_queues

    upstream = getattr(app.state, "upstream", None)
    if upstream is None:
        return 0
    queues = await _cups_queues(upstream)
    physical = [q for q in queues if not ordering.is_virtual(q.get("device_uri", ""))]
    found = evaluate(physical)
    await run_in_threadpool(apply, app.state.sessionmaker, found, [q["name"] for q in physical])
    return len(found)


async def run_printerwatch(app) -> None:
    await asyncio.sleep(45)
    while True:
        try:
            await check_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("printer check failed")
        await asyncio.sleep(CHECK_EVERY)
