"""Periodic maintenance print: a printer that sits idle for weeks dries its ink and glues its rollers, so an
administrator can ask the gateway to print a small page every N hours. One setting per queue, shared by every
administrator. Only physical printers: a virtual (PDF) queue has nothing to keep alive.

The page goes straight to CUPS under the gateway's own identity: it is not a user job, so it uses no quota and
ignores the block and maintenance switches (the point is the hardware's health), but it is recorded in the
administrators' audit when someone starts it and in the row's `last_*` fields when the scheduler does."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from spoolsteward.timeutil import aware, local_tz
from spoolsteward.config import settings
from spoolsteward.cups_client import list_queues
from spoolsteward.db.models import PrinterKeepalive, User
from spoolsteward.ipp import codec
from spoolsteward.policy import testpage
from spoolsteward.policy.ordering import key
from spoolsteward import i18n
from spoolsteward.i18n import N_, _, ngettext

log = logging.getLogger("spoolsteward.keepalive")
MIN_HOURS, MAX_HOURS, DEFAULT_HOURS = 1, 24 * 90, 24 * 7  # one week is what printer makers suggest for inkjets
PRESETS = [(24, N_("1 day")), (72, N_("3 days")), (168, N_("1 week (recommended)")), (336, N_("2 weeks")), (720, N_("30 days"))]
UNITS = {"hours": 1, "days": 24}
RETRY_AFTER = timedelta(hours=1)  # after a failed attempt
TICK = 60


def parse_interval(amount: str, unit: str) -> int:
    """Hours from the form's number + unit; ValueError (with a message for the admin) when out of range."""
    if unit not in UNITS:
        raise ValueError(_("Invalid unit of time"))
    if not (amount.strip().isascii() and amount.strip().isdigit()):
        raise ValueError(_("The interval must be a whole number"))
    hours = int(amount) * UNITS[unit]
    if not MIN_HOURS <= hours <= MAX_HOURS:
        raise ValueError(_("The interval must be between 1 hour and %(n)s days", n=MAX_HOURS // 24))
    return hours


def describe(hours: int) -> str:
    if hours % 24 == 0:
        d = hours // 24
        return ngettext("every day", "every %(n)s days", d)
    return ngettext("every hour", "every %(n)s hours", hours)


def split(hours: int) -> tuple[int, str]:
    """(number, unit) to show an interval in the form."""
    return (hours // 24, "days") if hours % 24 == 0 else (hours, "hours")


def settings_of(db: Session) -> dict[str, PrinterKeepalive]:
    return {r.printer: r for r in db.scalars(select(PrinterKeepalive))}


def save(db: Session, printer: str, hours: int, color: bool, admin: User) -> PrinterKeepalive:
    """Change the parameters only: whether the schedule runs is `set_enabled`'s business."""
    row = db.get(PrinterKeepalive, key(printer))
    if row is None:
        row = PrinterKeepalive(printer=key(printer))
        db.add(row)
    row.interval_hours, row.color = hours, color
    row.updated_at, row.updated_by = datetime.now(UTC), admin.username
    return row


def set_enabled(db: Session, printer: str, on: bool, admin: User) -> PrinterKeepalive:
    row = db.get(PrinterKeepalive, key(printer))
    if row is None:
        row = PrinterKeepalive(printer=key(printer), interval_hours=DEFAULT_HOURS, color=False)
        db.add(row)
    now = datetime.now(UTC)
    if on and not row.enabled:
        row.enabled_at = now  # the first page is due one interval from now, not immediately
        row.last_status = row.last_error = None
    row.enabled = on
    row.updated_at, row.updated_by = now, admin.username
    return row


def next_due(row: PrinterKeepalive) -> datetime | None:
    if not row.enabled:
        return None
    anchor = aware(row.last_ok_at) or aware(row.enabled_at)
    if anchor is None:
        return None
    due = anchor + timedelta(hours=row.interval_hours)
    tried = aware(row.last_attempt_at)
    if row.last_status == "error" and tried is not None:
        due = max(due, tried + RETRY_AFTER)
    return due


async def send(app, queue: str, pdf: bytes, title: str, *, color: bool) -> tuple[bool, str]:
    """Print-Job to CUPS as the gateway itself. (ok, message)."""
    req = codec.make_request(codec.PRINT_JOB, 1, printer_uri=f"ipp://localhost/printers/{queue}",
                             requesting_user_name=settings.cups_user_prefix + "maintenance",
                             job_name=title, document_format="application/pdf")
    req.groups.append(codec.Group(codec.TAG_JOB, [
        codec.Attr("print-color-mode", [codec.Val(codec.TAG_KEYWORD, "color" if color else "monochrome")])]))
    req.data = pdf
    try:
        resp = await app.state.upstream.post(
            f"/printers/{queue}", content=codec.encode(req), headers={"Content-Type": "application/ipp", "Host": "localhost"})
        out = codec.decode(resp.content) if resp.headers.get("content-type", "").startswith("application/ipp") else None
    except (httpx.HTTPError, codec.IppError) as exc:
        log.warning("maintenance print on %s: CUPS unreachable (%s)", queue, exc)
        return False, _("CUPS does not answer")
    if out is None or out.code >= 0x100:
        return False, _("CUPS refused the job (status %(code)s)", code="0x%04x" % out.code if out else _("unknown"))
    return True, "ok"


def maintenance_page(queue: str, title: str, color: bool, hours: int) -> bytes:
    now = datetime.now(local_tz())
    with i18n.using(testpage.pdf_language()):
        return testpage.build(
            _("Maintenance page"), _("%(title)s · queue %(queue)s · %(when)s · %(mode)s", title=title, queue=queue, when=f"{now:%Y-%m-%d %H:%M}",
                                    mode=_("colour") if color else _("black and white")),
            sections=[(_("What it is for"), [("", _("Automatic periodic print: keeps the print heads and rollers clean."))]),
                      (_("Schedule"), [(_("Interval"), describe(hours))])],
            swatches="color" if color else "mono", stripes=True)


def _record(sm, name: str, status: str, error: str | None, *, attempt: bool = True) -> None:
    with sm() as db:
        row = db.get(PrinterKeepalive, key(name))
        if row is None:
            return
        now = datetime.now(UTC)
        if attempt:
            row.last_attempt_at = now
        if status == "ok":
            row.last_ok_at = now
        row.last_status, row.last_error = status, (error or None) and error[:500]
        db.commit()


async def run_due(app) -> int:
    """One scheduler pass: print for every enabled, due, reachable physical queue. Returns how many pages were sent."""
    sm = app.state.sessionmaker
    rows = await run_in_threadpool(_enabled, sm)
    if not rows:
        await list_queues(app)  # still notes newly connected queues (their listing order)
        return 0
    queues = {key(q["name"]): q for q in await list_queues(app)}
    now, sent = datetime.now(UTC), 0
    for name, (hours, color, status, due) in rows.items():
        q = queues.get(name)
        if q is None or q.get("virtual") or due is None or now < due:
            continue
        if not q.get("online", True):
            if status != "offline":  # said once; tried again every pass until the printer answers
                await run_in_threadpool(_record, sm, name, "offline", "Printer not reachable", attempt=False)
            continue
        want_color = color and bool(q.get("color"))
        ok, msg = await send(app, q["name"], maintenance_page(q["name"], q.get("info") or q.get("model") or q["name"], want_color, hours),
                             "Pagina di manutenzione", color=want_color)
        await run_in_threadpool(_record, sm, name, "ok" if ok else "error", None if ok else msg)
        if ok:
            sent += 1
            log.info("maintenance page sent to %s (%s)", q["name"], "colour" if want_color else "mono")
        else:
            log.warning("maintenance page on %s failed: %s", q["name"], msg)
    return sent


def _enabled(sm) -> dict:
    with sm() as db:
        out = {}
        for r in db.scalars(select(PrinterKeepalive).where(PrinterKeepalive.enabled)):
            out[r.printer] = (r.interval_hours, r.color, r.last_status, next_due(r))
        return out


async def run_scheduler(app) -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await run_due(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("maintenance print scheduler failed")
        await asyncio.sleep(TICK)
