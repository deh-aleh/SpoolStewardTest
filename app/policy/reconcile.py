"""Sync job_log with what CUPS actually did: real page counts, and quota back for jobs
that never printed (canceled, aborted, Create-Job without a document)."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.db.models import JobLog, JobState
from app.ipp import codec

log = logging.getLogger(__name__)

STATE_CANCELED, STATE_ABORTED, STATE_COMPLETED = 7, 8, 9
LOOKBACK = timedelta(days=7)  # CUPS keeps finished jobs only for a while anyway
NEVER_SUBMITTED_AFTER = timedelta(hours=1)  # reserved but no CUPS job id: the gateway died mid-upload
PURGE_AFTER = timedelta(days=1)  # job vanished from CUPS history -> assume it ran
SAME_JOB_EARLY = 300.0   # seconds: CUPS made the job before we reserved it? only a clock difference can explain that
SAME_JOB_LATE = 3600.0   # the job is made when the upload starts: a slow upload or a long page count may delay it
_ATTRS = ["job-id", "job-state", "job-impressions-completed", "time-at-creation"]


async def fetch_jobs(upstream: httpx.AsyncClient, printer: str) -> dict[int, tuple[int, int, int | None]] | None:
    """{cups_job_id: (job-state, impressions_completed, time-at-creation)} for a queue, or None if CUPS failed."""
    req = codec.make_request(
        codec.GET_JOBS, 1, printer_uri=f"ipp://localhost/printers/{printer}", requesting_user_name="gateway"
    )
    ops = req.group(codec.TAG_OPERATION)
    ops.attrs.append(codec.Attr("which-jobs", [codec.Val(codec.TAG_KEYWORD, "all")]))
    ops.attrs.append(codec.Attr("requested-attributes", [codec.Val(codec.TAG_KEYWORD, k) for k in _ATTRS]))
    try:
        resp = await upstream.post(
            f"/printers/{printer}", content=codec.encode(req),
            headers={"Content-Type": "application/ipp", "Host": "localhost"},
        )
        msg = codec.decode(resp.content)
    except (httpx.HTTPError, codec.IppError) as exc:
        log.warning("reconcile: Get-Jobs failed for %s: %s", printer, exc)
        return None
    if msg.code >= 0x0100:
        log.warning("reconcile: Get-Jobs %s -> 0x%04x", printer, msg.code)
        return None
    jobs = {}
    for g in msg.groups:
        if g.tag != codec.TAG_JOB:
            continue
        jid, state, imp, born = g.get("job-id"), g.get("job-state"), g.get("job-impressions-completed"), g.get("time-at-creation")
        if jid and state:
            jobs[jid.first] = (state.first, (imp.first if imp else 0) or 0, born.first if born and isinstance(born.first, int) else None)
    return jobs


def is_same_job(row: JobLog, created: int | None) -> bool:
    """CUPS numbers its jobs from 1 again when its volume is recreated: a job number alone can then name a different, newer job.
    The creation time tells them apart. Unknown (an old CUPS, a test) = the same."""
    if created is None:
        return True
    ts = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=UTC)
    return -SAME_JOB_EARLY <= created - ts.timestamp() <= SAME_JOB_LATE


def apply_updates(session, rows: list[JobLog], cups: dict[str, dict[int, tuple] | None], now: datetime) -> int:
    changed = 0
    for row in rows:
        known = cups.get(row.printer)
        if known is None:
            continue  # CUPS unreachable for this queue: decide nothing
        info = known.get(row.cups_job_id)
        if info is not None and not is_same_job(row, info[2] if len(info) > 2 else None):
            info = None  # that number belongs to another job now: ours is gone from CUPS
        if info is None:
            ts = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=UTC)
            if now - ts > PURGE_AFTER:
                row.state = JobState.completed
                changed += 1
            continue
        state, imp = info[0], info[1]
        if state == STATE_COMPLETED:
            row.state, row.pages = JobState.completed, imp or row.pages  # keep the counted estimate if CUPS has none
        elif state in (STATE_CANCELED, STATE_ABORTED):
            if imp > 0:  # paper was used: it counts
                row.state, row.pages = JobState.completed, imp
            else:
                row.state = JobState.canceled if state == STATE_CANCELED else JobState.failed
                row.denied_reason = "canceled before printing" if state == STATE_CANCELED else "aborted by CUPS"
        else:
            continue  # still pending / processing / held
        changed += 1
    session.commit()
    return changed


def release_orphans(sm, now: datetime) -> int:
    """Quota slots reserved by a request that never reached CUPS (process killed, upload hung)."""
    with sm() as s:
        rows = list(s.scalars(select(JobLog).where(
            JobLog.state == JobState.accepted, JobLog.cups_job_id.is_(None),
            JobLog.ts < now - NEVER_SUBMITTED_AFTER,
        )))
        for r in rows:
            r.state, r.denied_reason = JobState.failed, "never reached CUPS"
        s.commit()
        return len(rows)


def _pending(sm, now: datetime) -> list[JobLog]:
    with sm() as s:
        return list(s.scalars(select(JobLog).where(
            JobLog.state.in_((JobState.accepted, JobState.held)),
            JobLog.cups_job_id.is_not(None), JobLog.printer.is_not(None), JobLog.ts >= now - LOOKBACK,
        )))


def _apply(sm, ids: list[int], cups, now) -> int:
    with sm() as s:
        rows = list(s.scalars(select(JobLog).where(JobLog.id.in_(ids))))
        return apply_updates(s, rows, cups, now)


async def reconcile_once(sm, upstream: httpx.AsyncClient, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    released = await run_in_threadpool(release_orphans, sm, now)
    pending = await run_in_threadpool(_pending, sm, now)
    if not pending:
        return released
    printers = {r.printer for r in pending}
    cups = {p: await fetch_jobs(upstream, p) for p in printers}
    return released + await run_in_threadpool(_apply, sm, [r.id for r in pending], cups, now)


async def run_forever(app, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            n = await reconcile_once(app.state.sessionmaker, app.state.upstream)
            if n:
                log.info("reconcile: %d job(s) updated", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reconcile failed")
