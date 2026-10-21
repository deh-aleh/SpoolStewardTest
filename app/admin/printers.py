"""Administrators: what the printers say about themselves (state, ink, lifetime counters, scanner), next to what
the gateway recorded. Everything is read live and best effort: a printer that does not offer a source just lacks
that section. Vendor counters come from HP's embedded web service when present (other brands: IPP data only)."""

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.timeutil import local_tz
from app.admin.common import back, current_admin, flash, get_db, render, verify_csrf
from app.audit.adminlog import record
from app.config import settings
from app.cups_client import is_online
from app.db.models import JobKind, JobLog, JobState, User
from app.ipp import codec
from app import i18n, notices
from app.i18n import N_, _, ngettext
from app.policy import control, discovery, keepalive, ordering, permissions, testpage

log = logging.getLogger("app.printers")
router = APIRouter(prefix="/admin")
_FAR = datetime.max.replace(tzinfo=UTC)
_CUPS_ATTRS = ["printer-name", "printer-info", "printer-state", "printer-state-reasons", "printer-is-accepting-jobs",
               "device-uri", "printer-make-and-model", "marker-names", "marker-levels", "marker-colors", "marker-types",
               "marker-low-levels", "queued-job-count", "printer-state-message", "printer-location", "color-supported"]
CUPS_ADMIN = "root"  # CUPS' SystemGroup: it trusts this name on its private socket, and only the gateway reaches the socket
DEBUG_PPD = "lsb/usr/cups-pdf/CUPS-PDF_noopt.ppd"
NAME_RE = permissions.NAME_RE
STATES = {3: N_("idle"), 4: N_("printing"), 5: N_("stopped")}  # shown through _(): the cached data stays language-neutral
LEDM_USAGE = {"TotalImpressions": N_("Pages printed (total)"), "MonochromeImpressions": N_("of which black and white"),
              "ColorImpressions": N_("of which in colour"), "SimplexSheets": N_("Single-sided sheets"),
              "DuplexSheets": N_("Double-sided sheets"), "JamEvents": N_("Jams"), "MispickEvents": N_("Paper pick failures")}


def _dyn(text: str) -> str:
    """Translate a text only known at run time (its msgid is registered with N_ where it is defined)."""
    return _(text)


def _vals(g: codec.Group, name: str) -> list:
    a = g.get(name)
    return [v.value for v in a.values] if a else []


def _tag(text: str, name: str) -> str:
    m = re.search(rf"<(?:\w+:)?{name}(?:\s[^>]*)?>([^<]*)<", text)
    return m.group(1).strip() if m else ""


def _ink(g: codec.Group) -> list[dict]:
    """Ink levels from the marker-* attributes of a CUPS queue or of the printer itself."""
    levels, names = _vals(g, "marker-levels"), _vals(g, "marker-names")
    return [{"name": n, "level": lv, "color": c if isinstance(c, str) and c.startswith("#") else "#888",
             "low": lv <= low if isinstance(lv, int) and isinstance(low, int) and lv >= 0 else False}
            for n, lv, c, low in zip(names, levels, _vals(g, "marker-colors") + ["#888"] * 8, _vals(g, "marker-low-levels") + [-1] * 8)]


async def _cups_queues(upstream: httpx.AsyncClient) -> list[dict]:
    req = codec.make_request(0x4002, 1, requesting_user_name="gateway")
    req.group(codec.TAG_OPERATION).attrs.append(
        codec.Attr("requested-attributes", [codec.Val(codec.TAG_KEYWORD, a) for a in _CUPS_ATTRS]))
    resp = await upstream.post("/", content=codec.encode(req), headers={"Content-Type": "application/ipp", "Host": "localhost"})
    out = []
    for g in codec.decode(resp.content).groups:
        if g.tag != codec.TAG_PRINTER:
            continue
        first = lambda n, d="": next(iter(_vals(g, n)), d)  # noqa: E731
        ink = _ink(g)
        out.append({
            "name": first("printer-name"), "info": first("printer-info"), "model": first("printer-make-and-model"),
            "state": STATES.get(first("printer-state", 3), "?"), "reasons": [r for r in _vals(g, "printer-state-reasons") if r != "none"],
            "accepting": first("printer-is-accepting-jobs", True), "device_uri": first("device-uri"),
            "queued": first("queued-job-count", 0), "message": first("printer-state-message"), "location": first("printer-location"),
            "ink": ink, "color": bool(first("color-supported", False)),
        })
    return out


async def _device(base: str) -> dict:
    """Ask the printer itself (IPP, HP counters, eSCL). Each part is independent and may be missing."""
    d: dict = {"ipp": {}, "usage": [], "config": {}, "scanner": None}
    async with httpx.AsyncClient(timeout=6.0, verify=False, trust_env=False) as c:
        async def ipp():
            req = codec.make_request(codec.GET_PRINTER_ATTRIBUTES, 1, printer_uri=base + "/ipp/print")
            req.group(codec.TAG_OPERATION).attrs.append(codec.Attr("requested-attributes", [codec.Val(codec.TAG_KEYWORD, "all")]))
            r = await c.post(base + "/ipp/print", content=codec.encode(req), headers={"Content-Type": "application/ipp"})
            g = next(g for g in codec.decode(r.content).groups if g.tag == codec.TAG_PRINTER)
            first = lambda n, dflt="": next(iter(_vals(g, n)), dflt)  # noqa: E731
            d["ipp"] = {
                "firmware": first("printer-firmware-string-version"), "uuid": first("printer-uuid"),
                "ppm": first("pages-per-minute", None), "ppm_color": first("pages-per-minute-color", None),
                "media": [m for m in _vals(g, "media-ready") if isinstance(m, str)],
                "alerts": [a for a in _vals(g, "printer-alert-description") if isinstance(a, str) and a],
                "info": first("printer-info"), "more": first("printer-more-info"), "ink": _ink(g),
            }

        async def usage():
            r = await c.get(base + "/DevMgmt/ProductUsageDyn.xml")
            if r.status_code == 200 and "TotalImpressions" in r.text:
                d["usage"] = [(label, int(v)) for tag, label in LEDM_USAGE.items() if (v := _tag(r.text, tag)).isdigit()]
                ml = re.search(r"<(?:\w+:)?CumulativeMarkingAgentUsed[^>]*>\s*<[^>]*ValueFloat>([\d.]+)<", r.text)
                if ml:
                    d["usage"].append((N_("Ink used (total)"), f"{float(ml.group(1)):.0f} ml"))

        async def config():
            r = await c.get(base + "/DevMgmt/ProductConfigDyn.xml")
            if r.status_code == 200:
                d["config"] = {"serial": _tag(r.text, "SerialNumber"), "sku": _tag(r.text, "SKUIdentifier"),
                               "model": _tag(r.text, "MakeAndModel")}

        async def escl():
            r = await c.get(base + "/eSCL/ScannerStatus")
            if r.status_code == 200:
                cap = await c.get(base + "/eSCL/ScannerCapabilities")
                t = cap.text if cap.status_code == 200 else ""
                d["scanner"] = {"state": _tag(r.text, "State"), "adf": _tag(r.text, "AdfState"),
                                "dpi": sorted({int(x) for x in re.findall(r"<scan:XResolution>(\d+)<", t)}),
                                "formats": sorted(set(re.findall(r"<pwg:DocumentFormat>([^<]+)<", t)) - {"application/octet-stream"})}

        await asyncio.gather(ipp(), usage(), config(), escl(), return_exceptions=True)
    return d


def _gateway_stats(db: Session) -> dict:
    rows = db.execute(select(JobLog.printer, JobLog.kind, func.count(JobLog.id), func.coalesce(func.sum(JobLog.pages), 0),
                             func.max(JobLog.ts)).where(JobLog.state.in_((JobState.accepted, JobState.completed, JobState.held)))
                      .group_by(JobLog.printer, JobLog.kind)).all()
    out: dict = {}
    for printer, kind, n, pages, last in rows:
        e = out.setdefault(printer, {"jobs": 0, "pages": 0, "scans": 0, "last": None})
        if kind is JobKind.scan:
            e["scans"] += n
        else:
            e["jobs"] += n
            e["pages"] += int(pages)
        e["last"] = max(filter(None, (e["last"], last)), default=None)
    return out


async def gather(app, db_stats) -> list[dict]:
    cached = getattr(app.state, "printers_cache", None)
    if cached and time.monotonic() - cached[0] < settings.printers_refresh_seconds:
        queues = cached[1]
    else:
        try:
            queues = await _cups_queues(app.state.upstream)
        except (httpx.HTTPError, codec.IppError, StopIteration) as exc:
            log.warning("could not list CUPS queues: %s", exc)
            queues = []
        for q, up in zip(queues, await asyncio.gather(*[is_online(q["device_uri"], getattr(app.state, "usb_present", None)) for q in queues])):
            q["online"] = up  # a queue outlives the cable: ask the device itself, not CUPS' memory of it
        groups: dict[str, dict] = {}
        for q in queues:
            uri = urlsplit(q["device_uri"])
            real = uri.scheme in ("ipp", "ipps", "http", "https") and uri.hostname and uri.hostname not in ("localhost", "127.0.0.1")
            key = f"{uri.hostname}:{uri.port}" if real else f"virtual:{q['name']}"
            g = groups.setdefault(key, {"key": key, "queues": [], "base": f"http://{uri.hostname}:{uri.port or 631}" if real else None,
                                        "virtual": ordering.is_virtual(q["device_uri"])})
            g["queues"].append(q)
        devices = list(groups.values())
        for g in devices:
            g["online"] = any(q["online"] for q in g["queues"])
        results = await asyncio.gather(*[_device(g["base"]) if g["base"] and g["online"] else asyncio.sleep(0, {}) for g in devices])
        for g, extra in zip(devices, results):
            g["device"] = extra
            # CUPS only learns the ink levels after it has printed on a queue (a queue just created has none): the printer
            # itself always knows, so use what it says when the queue has nothing
            if extra and extra.get("ipp", {}).get("ink"):
                for q in g["queues"]:
                    q["ink"] = q["ink"] or extra["ipp"]["ink"]
        queues = devices
        app.state.printers_cache = (time.monotonic(), queues)
    stats = await run_in_threadpool(db_stats)
    seen = await run_in_threadpool(ordering.note_seen, app.state.sessionmaker, [q["name"] for g in queues for q in g["queues"]])
    out = []
    legacy = stats.get("scanner")  # scans recorded before the audit kept the device name: shown on the first real device
    for g in queues:
        first = g["queues"][0]
        scans = [stats[q["name"]] for q in g["queues"] if q["name"] in stats and stats[q["name"]]["scans"]]
        if g["base"] and legacy is not None:
            scans.append(legacy)
            legacy = None
        scan_stats = {"scans": sum(x["scans"] for x in scans), "last": max(filter(None, (x["last"] for x in scans)), default=None)} \
            if scans else None
        out.append({**g, "title": (g["device"].get("config", {}).get("model") if g.get("device") else "") or first["model"] or first["name"],
                    "stats": {q["name"]: stats.get(q["name"], {"jobs": 0, "pages": 0, "scans": 0, "last": None}) for q in g["queues"]},
                    "scan_stats": scan_stats})
    out.sort(key=lambda d: (d["virtual"], min((seen.get(ordering.key(q["name"])) or _FAR for q in d["queues"]), default=_FAR),
                            d["queues"][0]["name"].lower()))  # physical first, then oldest connection (see policy/ordering)
    return out


async def _cups_admin(request: Request, req: codec.IppMessage) -> bool:
    """Send an administrative IPP request to CUPS as its system user. True when CUPS accepted it."""
    try:
        resp = await request.app.state.upstream.post("/admin/", content=codec.encode(req),
                                                      headers={"Content-Type": "application/ipp", "Host": "localhost"})
        ok = resp.headers.get("content-type", "").startswith("application/ipp") and codec.decode(resp.content).code < 0x100
    except (httpx.HTTPError, codec.IppError):
        log.exception("CUPS admin request failed")
        return False
    request.app.state.printers_cache = None
    request.app.state.queues_cache = None
    request.app.state.queue_caps_cache = None
    return ok


@router.post("/printers/debug", dependencies=[Depends(verify_csrf)])
async def debug_create(request: Request, admin: User = Depends(current_admin)):
    """A throw-away printer that writes PDFs instead of paper (CUPS-PDF): named debugprinter0, debugprinter1, ..."""
    try:
        taken = {q["name"].lower() for q in await _cups_queues(request.app.state.upstream)}
    except (httpx.HTTPError, codec.IppError, StopIteration):
        flash(request, _("CUPS does not answer"), "err")
        return back("/admin/printers")
    n = 0
    while f"debugprinter{n}" in taken:
        n += 1
    name = f"debugprinter{n}"
    req = codec.make_request(0x4003, 1, printer_uri=f"ipp://localhost/printers/{name}", requesting_user_name=CUPS_ADMIN)
    req.group(codec.TAG_OPERATION).attrs.append(codec.Attr("ppd-name", [codec.Val(codec.TAG_NAME, DEBUG_PPD)]))
    req.groups.append(codec.Group(codec.TAG_PRINTER, [
        codec.Attr("device-uri", [codec.Val(codec.TAG_URI, "cups-pdf:/")]),
        codec.Attr("printer-is-accepting-jobs", [codec.Val(codec.TAG_BOOLEAN, True)]),
        codec.Attr("printer-state", [codec.Val(codec.TAG_ENUM, 3)]),
        codec.Attr("printer-info", [codec.Val(codec.TAG_TEXT, f"Test printer ({name})")]),
    ]))
    ok = await _cups_admin(request, req)
    if ok:
        def note():
            with request.app.state.sessionmaker() as db:
                discovery.took_name(db, name)
                record(db, db.get(User, admin.id), "printer.debug_create", "printer", None, printer=name)
                db.commit()
        await run_in_threadpool(note)
    flash(request, _("Test printer %(name)s created: it writes PDFs, it uses no paper", name=name) if ok else _("CUPS did not accept the request"), "ok" if ok else "err")
    return back("/admin/printers")


@router.post("/printers/{name}/delete", dependencies=[Depends(verify_csrf)])
async def printer_delete(request: Request, name: str, admin: User = Depends(current_admin)):
    """Virtual (CUPS-PDF) queues, and the queue of a real printer that does not answer (unplugged for good). A printer
    that answers is not deleted from here: that would be a click on the wrong card."""
    if not NAME_RE.match(name):
        raise HTTPException(404)
    try:
        queue = next((q for q in await _cups_queues(request.app.state.upstream) if q["name"] == name), None)
    except (httpx.HTTPError, codec.IppError, StopIteration):
        queue = None
    if queue is None:
        flash(request, _("%(name)s: printer not found", name=name), "err")
        return back("/admin/printers")
    if not ordering.is_virtual(queue["device_uri"]) and await is_online(queue["device_uri"], getattr(request.app.state, "usb_present", None)):
        flash(request, _("%(name)s answers: only virtual printers and printers that do not answer can be deleted from here", name=name), "err")
        return back("/admin/printers")
    req = codec.make_request(0x4004, 1, printer_uri=f"ipp://localhost/printers/{name}", requesting_user_name=CUPS_ADMIN)
    ok = await _cups_admin(request, req)
    if ok:
        def note():
            with request.app.state.sessionmaker() as db:
                discovery.forget_queue(db, name, "deleted")
                record(db, db.get(User, admin.id), "printer.delete", "printer", None, printer=name)
                db.commit()
        await run_in_threadpool(note)
    flash(request, _("%(name)s deleted", name=name) if ok else _("CUPS did not accept the request"), "ok" if ok else "err")
    return back("/admin/printers")


@router.post("/printers/former/forget", dependencies=[Depends(verify_csrf)])
async def printer_forget_name(request: Request, name: str = Form(...), admin: User = Depends(current_admin)):
    """Take a name of the past out of the list of the rename dialog (and out of the database)."""
    name = name.strip()

    def forget():
        with request.app.state.sessionmaker() as db:
            found = discovery.forget_former(db, name)
            if found:
                record(db, db.get(User, admin.id), "printer.forget_name", "printer", None, printer=name)
            db.commit()
            return found
    found = await run_in_threadpool(forget)
    if request.headers.get("x-fetch"):  # the page removes the name itself, no reload and no message
        return JSONResponse({"ok": True, "found": found})
    flash(request, _("The name %(name)s is forgotten", name=name) if found else _("%(name)s was not in the list", name=name),
          "ok" if found else "err")
    return back("/admin/printers")


@router.post("/printers/{name}/move", dependencies=[Depends(verify_csrf)])
async def printer_move(request: Request, name: str, direction: str = Form(...), admin: User = Depends(current_admin)):
    """One place up or down in the list of printers, for the administrator who clicks (everybody has their own order; it
    is also the order of their print form). A printer moves only inside its group: physical printers stay above the
    virtual ones."""
    if not NAME_RE.match(name) or direction not in ("up", "down"):
        raise HTTPException(404)
    cards = await _devices(request, admin)
    idx = next((i for i, d in enumerate(cards) if any(q["name"] == name for q in d["queues"])), None)
    if idx is None:
        flash(request, _("%(name)s: printer not found", name=name), "err")
        return back("/admin/printers")
    other = idx + (-1 if direction == "up" else 1)
    if not 0 <= other < len(cards) or cards[other]["virtual"] != cards[idx]["virtual"]:
        return back("/admin/printers")  # already at the end of its group (the page shows the button disabled)
    sm = request.app.state.sessionmaker
    await run_in_threadpool(ordering.reorder, sm, admin.id, [[q["name"] for q in d["queues"]] for d in cards], idx, other)
    await run_in_threadpool(_note, request, admin, "printer.move", printer=name, direction=direction)
    request.app.state.printers_cache = request.app.state.queues_cache = None
    return back("/admin/printers")


class _Refuse(Exception):
    """A form problem the administrator is told about with a message on the list of printers."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


async def _rename_target(request: Request, name: str, new_name: str) -> tuple[dict, str]:
    """The queue to rename and the driver it keeps; raises _Refuse when it may not be renamed (nothing is changed yet)."""
    try:
        queues = await _cups_queues(request.app.state.upstream)
    except (httpx.HTTPError, codec.IppError, StopIteration):
        raise _Refuse(_("CUPS does not answer")) from None
    queue = next((q for q in queues if q["name"] == name), None)
    if queue is None:
        raise _Refuse(_("%(name)s: printer not found", name=name))
    if new_name.lower() == name.lower():
        raise _Refuse(_("That is already its name"))
    if any(q["name"].lower() == new_name.lower() for q in queues):
        raise _Refuse(_("A queue called %(name)s already exists", name=new_name))
    if queue["queued"]:
        raise _Refuse(_("%(name)s has jobs waiting: rename it when the queue is empty", name=name))
    ppd = await discovery.driver_for(request.app.state.upstream, queue["device_uri"], DEBUG_PPD)
    if ppd is None:
        raise _Refuse(_("%(name)s can not be renamed: its driver is not known to the gateway", name=name))
    return queue, ppd


async def _rename_in_cups(request: Request, name: str, new_name: str, queue: dict, ppd: str) -> None:
    """The new queue first, the old one removed only after that; raises _Refuse when CUPS does not accept."""
    info = queue["info"].replace(name, new_name) if queue["info"] else new_name
    if not await discovery.add_queue(request.app, new_name, queue["device_uri"], info, ppd, queue["location"]):
        raise _Refuse(_("CUPS did not accept the request"))
    if not await discovery.delete_queue(request.app, name):
        if not await discovery.delete_queue(request.app, new_name):  # back to how it was
            log.warning("rename %s -> %s refused and the copy %s could not be removed: remove it by hand", name, new_name, new_name)
        raise _Refuse(_("CUPS did not accept the request"))


def _rename_records(request: Request, admin: User, name: str, new_name: str, notify: bool) -> None:
    """What the gateway keeps under the name follows the queue."""
    with request.app.state.sessionmaker() as db:
        ordering.rename(db, name, new_name)
        lists = permissions.rename_printer(db, name, new_name)  # the printer lists of groups and users follow the queue
        discovery.note_former(db, name, "renamed")
        discovery.took_name(db, new_name)
        record(db, db.get(User, admin.id), "printer.rename", "printer", None, printer=name, new_name=new_name, permission_lists=lists,
               notified_users=notify)
        db.commit()


@router.post("/printers/{name}/rename", dependencies=[Depends(verify_csrf)])
async def printer_rename(request: Request, name: str, new_name: str = Form(...), notify: str = Form(""), admin: User = Depends(current_admin)):
    """Give a queue another name. CUPS can not rename a queue, so the gateway makes the new one on the same device (same
    driver, same description) and only then removes the old one. What the gateway keeps under the name (block, maintenance
    print, places in the lists, the printers allowed to groups and users) follows. Devices that were set up by hand with the
    old address stop printing, unless a printer is later given that old name again (see `discovery.former_names`).
    `notify`: tick in the confirmation window, puts a notice in the bell of every active account (the administrator who renamed excluded)."""
    new_name = new_name.strip()
    if not NAME_RE.match(name):
        raise HTTPException(404)
    if not NAME_RE.match(new_name):
        flash(request, _("Queue names use letters, digits and . _ @ + - only (max 127)"), "err")
        return back("/admin/printers")
    try:
        queue, ppd = await _rename_target(request, name, new_name)
        await _rename_in_cups(request, name, new_name, queue, ppd)
    except _Refuse as stop:
        flash(request, stop.message, "err")
        return back("/admin/printers")
    await run_in_threadpool(_rename_records, request, admin, name, new_name, bool(notify))
    if notify:
        await run_in_threadpool(notices.notify_all_users, request.app.state.sessionmaker, "printer_renamed",
                                {"old": name, "new": new_name}, new_name, admin.id)
    request.app.state.printers_cache = request.app.state.queues_cache = None
    flash(request, _("%(old)s is now called %(new)s. Computers and phones set up by hand with the old address must be set up again.", old=name, new=new_name), "ok")
    return back("/admin/printers")


@router.post("/printers/discover", dependencies=[Depends(verify_csrf)])
async def printers_discover(request: Request, admin: User = Depends(current_admin)):
    """Look for new USB printers right now instead of waiting for the next pass."""
    out = await discovery.run_once(request.app)
    if out["created"]:
        flash(request, ngettext("%(n)s new printer added", "%(n)s new printers added", out["created"], n=out["created"]), "ok")
    for note in out["notes"]:
        flash(request, _discovery_note(note), "err")
    if not out["created"] and not out["notes"]:
        flash(request, _("No new printer found"), "ok")
    return back("/admin/printers")


def _discovery_note(note: dict) -> str:
    if note.get("kind") == "no_driver":
        return _("USB printer %(model)s found, but CUPS has no driver for it: it can not be added", model=note.get("model", "?"))
    return _("CUPS did not accept the new printer %(model)s", model=note.get("model", "?"))


async def _devices(request: Request, admin: User | None = None) -> list[dict]:
    """The printer cards. With `admin`, in the order that administrator chose (everybody has their own)."""
    def stats():
        with request.app.state.sessionmaker() as s:
            return _gateway_stats(s)

    cards = await gather(request.app, stats)
    if admin is None:
        return cards
    pos = await run_in_threadpool(ordering.positions, request.app.state.sessionmaker, admin.id)
    return ordering.arrange(cards, pos, name=lambda d: min((q["name"] for q in d["queues"]), key=lambda n: pos.get(ordering.key(n), 1 << 30)),
                            uri=lambda d: "cups-pdf:/" if d["virtual"] else "x:")


async def _physical(request: Request, name: str) -> tuple[dict, dict] | None:
    """(card, queue) for a physical printer's queue, or None after flashing why not: the maintenance buttons exist
    only for real hardware, and the server checks it too, not just the page."""
    if not NAME_RE.match(name):
        raise HTTPException(404)
    for d in await _devices(request):
        for q in d["queues"]:
            if q["name"] == name:
                if d["virtual"]:
                    flash(request, _("%(name)s is a virtual printer: maintenance is only for physical ones", name=name), "err")
                    return None
                return d, q
    flash(request, _("%(name)s: printer not found", name=name), "err")
    return None


def _note(request: Request, admin: User, action: str, **detail) -> None:
    with request.app.state.sessionmaker() as db:
        record(db, db.get(User, admin.id), action, "printer", None, **detail)
        db.commit()


@router.post("/printers/{name}/keepalive", dependencies=[Depends(verify_csrf)])
async def keepalive_save(request: Request, name: str, amount: str = Form(""), unit: str = Form("days"), color: str = Form(""),
                         admin: User = Depends(current_admin)):
    """Change the parameters of the periodic maintenance print. Does NOT switch it on or off (that is the toggle)."""
    found = await _physical(request, name)
    if found is None:
        return back("/admin/printers")
    try:
        hours = keepalive.parse_interval(amount, unit)
    except ValueError as exc:
        flash(request, str(exc), "err")
        return back("/admin/printers")
    want_color = color == "1"
    if want_color and not found[1]["color"]:
        flash(request, _("%(name)s does not print in colour", name=name), "err")
        return back("/admin/printers")

    def write():
        with request.app.state.sessionmaker() as db:
            keepalive.save(db, name, hours, want_color, db.get(User, admin.id))
            record(db, db.get(User, admin.id), "printer.keepalive_save", "printer", None, printer=name, hours=hours, color=want_color)
            db.commit()
    await run_in_threadpool(write)
    flash(request, _("%(name)s: periodic print %(every)s, %(mode)s. The settings are saved; turn it on or off with the switch",
                     name=name, every=keepalive.describe(hours), mode=_("in colour") if want_color else _("in black and white")), "ok")
    return back("/admin/printers")


@router.post("/printers/{name}/keepalive/toggle", dependencies=[Depends(verify_csrf)])
async def keepalive_toggle(request: Request, name: str, on: str = Form("0"), admin: User = Depends(current_admin)):
    found = await _physical(request, name)
    if found is None:
        return back("/admin/printers")
    flag = on == "1"

    def write():
        with request.app.state.sessionmaker() as db:
            row = keepalive.set_enabled(db, name, flag, db.get(User, admin.id))
            record(db, db.get(User, admin.id), "printer.keepalive_on" if flag else "printer.keepalive_off", "printer", None,
                   printer=name, hours=row.interval_hours)
            db.commit()
            return keepalive.describe(row.interval_hours)
    every = await run_in_threadpool(write)
    flash(request, _("%(name)s: periodic print turned on (%(every)s)", name=name, every=every) if flag else _("%(name)s: periodic print turned off", name=name), "ok")
    return back("/admin/printers")


def _test_page(card: dict, queue_name: str, color: bool) -> bytes:
    """The same figures the page shows for this printer, on paper."""
    dev = card.get("device") or {}
    ipp, cfg, first = dev.get("ipp") or {}, dev.get("config") or {}, card["queues"][0]
    when = lambda t: t.replace(tzinfo=t.tzinfo or UTC).astimezone(local_tz()).strftime("%Y-%m-%d %H:%M") if t else "—"  # noqa: E731
    now = datetime.now(local_tz())
    state = [(q["name"], (_dyn(q["state"]) if q.get("online") else _("NOT REACHABLE")) + ", " + _("%(n)s in queue", n=q["queued"])
              + ("" if q["accepting"] else ", " + _("not accepting jobs"))) for q in card["queues"]]
    gw = [(name, _("%(jobs)s jobs, %(pages)s pages, last %(when)s", jobs=s["jobs"], pages=s["pages"], when=when(s["last"])))
          for name, s in card["stats"].items()]
    if card.get("scan_stats"):
        gw.append((_("scans"), _("%(n)s, last %(when)s", n=card["scan_stats"]["scans"], when=when(card["scan_stats"]["last"]))))
    speed = _("%(n)s pages/min", n=ipp["ppm"]) + (f" ({_('colour %(n)s', n=ipp['ppm_color'])})" if ipp.get("ppm_color") else "") if ipp.get("ppm") else ""
    sc = dev.get("scanner") or {}
    return testpage.build(
        _("Test print · %(title)s", title=card["title"]),
        _("queue %(name)s · %(when)s · %(mode)s", name=queue_name, when=f"{now:%Y-%m-%d %H:%M}", mode=_("colour") if color else _("black and white")),
        ink=first["ink"] if card.get("online") else [], ink_title=_("Ink left"),
        sections=[(_("State"), state), (_("Printer counters"), [(_dyn(label), str(v)) for label, v in dev.get("usage", [])]),
                  (_("From the gateway"), gw),
                  (_("Details"), [(_("Serial number"), cfg.get("serial")), ("Firmware", ipp.get("firmware")), (_("Model"), cfg.get("sku")),
                                  (_("Speed"), speed), (_("Paper ready"), ", ".join(ipp.get("media") or [])),
                                  (_("Location"), first.get("location")), (_("Connection"), card.get("base")),
                                  (_("Alerts"), " · ".join(dict.fromkeys(ipp.get("alerts") or [])))]),
                  (_("Scanner"), [(_("State"), sc.get("state")), (_("Feeder"), sc.get("adf")),
                                  (_("Resolutions"), ", ".join(f"{d} dpi" for d in sc.get("dpi", []))),
                                  (_("Formats"), ", ".join(sc.get("formats", [])))] if sc else [])],
        swatches="")


@router.post("/printers/{name}/testprint", dependencies=[Depends(verify_csrf)])
async def test_print(request: Request, name: str, mode: str = Form(""), admin: User = Depends(current_admin)):
    """Print the printer's own statistics on the printer itself, in black and white or in colour."""
    if mode not in ("mono", "color"):
        raise HTTPException(400, "invalid mode")
    found = await _physical(request, name)
    if found is None:
        return back("/admin/printers")
    card, queue = found
    if not queue.get("online"):
        flash(request, _("%(name)s does not answer (off or unplugged?): nothing to print", name=name), "err")
        return back("/admin/printers")
    if mode == "color" and not queue["color"]:
        flash(request, _("%(name)s does not print in colour", name=name), "err")
        return back("/admin/printers")
    with i18n.using(testpage.pdf_language()):
        pdf = _test_page(card, name, mode == "color")
    ok, msg = await keepalive.send(request.app, name, pdf, "Test print", color=mode == "color")
    if ok:
        await run_in_threadpool(_note, request, admin, "printer.test_print", printer=name, color=mode == "color")
    flash(request, _("Test print sent to %(name)s (%(mode)s)", name=name, mode=_("colour") if mode == "color" else _("black and white")) if ok
          else f"{name}: {msg}", "ok" if ok else "err")
    return back("/admin/printers")


@router.get("/printers", response_class=HTMLResponse)
async def printers(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    def switches():
        with request.app.state.sessionmaker() as s:
            ka = keepalive.settings_of(s)
            for r in ka.values():
                s.expunge(r)
            return control.controls(s), ka

    devices = await _devices(request, admin)
    controls, ka = await run_in_threadpool(switches)
    # the address computers use for a queue: the one this page was opened with (same server, same port as the portal)
    ipp_base = f"{settings.public_scheme or 'ipps'}://{request.headers.get('host', 'localhost')}"
    return render(request, "printers.html", admin, devices=devices, states=STATES, controls=controls, key=control.key, ipp_base=ipp_base,
                  keepalive=ka, next_due=keepalive.next_due, describe=keepalive.describe, split=keepalive.split,
                  presets=keepalive.PRESETS, now=datetime.now(UTC), is_usb=discovery.is_usb_queue, transport=discovery.transport,
                  former=discovery.former_names(db, [q["name"] for d in devices for q in d["queues"]]),
                  offline_since=await run_in_threadpool(discovery.offline_since, request.app.state.sessionmaker),
                  remove_after=discovery.remove_after(), refresh_seconds=settings.printers_refresh_seconds, remove_hours="%g" % settings.offline_remove_hours, notes=[_discovery_note(n) for n in getattr(request.app.state, "discovery", {}).get("notes", [])])
