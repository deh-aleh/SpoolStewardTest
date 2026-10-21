"""Printers that find themselves, and queues that leave by themselves.

USB printers reach CUPS in one of two ways, tried in this order:

  1. the ipp-usb bridge: a printer with IPP-over-USB gets a port (60000-60099) on the bridge container, and its queue
     points at `ipp://ipp-usb:<port>/ipp/print` (driverless, preferred: the device is served by ipp-usb);
  2. CUPS' own usb backend, for printers that have no IPP-over-USB (most older lasers): the queue points at
     `usb://Maker/Model?serial=...` and needs a driver (PPD) that CUPS has. A device that ipp-usb serves is never
     taken this way.

Every pass (a few seconds' work every `SPOOLSTEWARD_DISCOVERY_INTERVAL`) creates the queue of each new device, notes which USB
queues do not answer, and removes the queue of a USB device that stayed unreachable for `SPOOLSTEWARD_OFFLINE_REMOVE_HOURS`.
Queues of network printers are never removed by themselves: a printer switched off for the weekend must come back as it was
(blocks, maintenance print). An administrator can still delete an unreachable queue by hand from the Printers page.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.timeutil import aware
from app.audit.adminlog import record
from app.config import settings
from app.cups_client import list_queues
from app.db.models import PrinterControl, PrinterFormer, PrinterKeepalive, PrinterOffline
from app.ipp import codec
from app.policy import ordering

log = logging.getLogger("app.discovery")
CUPS_ADMIN = "root"  # CUPS' SystemGroup: it trusts this name on its private socket, and only the gateway reaches the socket
CUPS_GET_DEVICES, CUPS_GET_PPDS, CUPS_ADD_MODIFY_PRINTER, CUPS_DELETE_PRINTER = 0x400B, 0x400C, 0x4003, 0x4004
IPP_HEADERS = {"Content-Type": "application/ipp", "Host": "localhost"}
_PROBES = 20  # simultaneous probes of the bridge's ports


def is_usb_queue(device_uri: str) -> bool:
    """A queue whose printer hangs on a USB cable: the only ones removed by themselves when the device is gone."""
    u = urlsplit(device_uri)
    return u.scheme == "usb" or bool(settings.usb_bridge_host and u.hostname and u.hostname.lower() == settings.usb_bridge_host.lower())


def bridge_ports() -> list[int]:
    m = re.fullmatch(r"\s*(\d{1,5})\s*-\s*(\d{1,5})\s*", settings.usb_bridge_ports or "")
    lo, hi = (int(m.group(1)), int(m.group(2))) if m else (60000, 60099)
    return list(range(lo, min(hi, lo + 199) + 1))


def remove_after() -> timedelta | None:
    return timedelta(hours=settings.offline_remove_hours) if settings.offline_remove_hours > 0 else None


def parse_device_id(text: str) -> dict[str, str]:
    """IEEE 1284 id 'MFG:Brother;MDL:DCP-7055;...' -> {'mfg': 'Brother', 'mdl': 'DCP-7055'} (keys lower-case)."""
    out: dict[str, str] = {}
    for part in (text or "").split(";"):
        k, sep, v = part.partition(":")
        if sep:
            out[{"manufacturer": "mfg", "model": "mdl", "serialnumber": "sn", "sern": "sn"}.get(k.strip().lower(), k.strip().lower())] = v.strip()
    return out


QUEUE_NAME_MAX = 24
_MAKER_ALIASES = {"hewlett-packard": "HP", "hewlett packard": "HP", "hp inc.": "HP", "hp inc": "HP"}
_NOISE = {"series", "printer", "printers", "mfp", "all-in-one", "aio", "multifunction", "wireless", "using", "driver"}


def queue_name(model: str, taken: set[str], limit: int = QUEUE_NAME_MAX) -> str:
    """A short queue name made of the model: 'HP Smart Tank 7300 series [A7E76E]' -> 'HP_Smart_Tank_7300'.
    Drops what says nothing ('series', 'printer', 'MFP', a bracketed part number, anything after a comma), writes the maker
    short ('Hewlett-Packard' -> 'HP') and, when it is still longer than `limit`, keeps the maker and the words that carry
    a digit (the model code). Made unique with a number."""
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", model.split(",")[0])
    for long, short in _MAKER_ALIASES.items():
        text = re.sub(rf"^{re.escape(long)}\b", short, text.strip(), flags=re.I)
    words = [w for w in text.split() if w.lower() not in _NOISE]
    if len("_".join(words)) > limit and len(words) > 2:
        words = [words[0]] + [w for w in words[1:] if any(c.isdigit() for c in w)] or words
    base = re.sub(r"[^A-Za-z0-9._@+-]+", "_", "_".join(words)).strip("_.-")[:limit].rstrip("_.-") or "printer"
    name, n = base, 2
    while name.lower() in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def transport(device_uri: str) -> str | None:
    """How a printer is wired: 'IPP-USB' (through the ipp-usb bridge), 'USB' (CUPS' own usb backend) or None (network, virtual)."""
    u = urlsplit(device_uri)
    if u.scheme == "usb":
        return "USB"
    if settings.usb_bridge_host and u.hostname and u.hostname.lower() == settings.usb_bridge_host.lower():
        return "IPP-USB"
    return None


def _vals(g: codec.Group, name: str) -> list:
    a = g.get(name)
    return [v.value for v in a.values] if a else []


def _first(g: codec.Group, name: str, default=""):
    return next(iter(_vals(g, name)), default)


# --- what is there --------------------------------------------------------------------------------------------------

async def _ask_bridge(client: httpx.AsyncClient, sem: asyncio.Semaphore, port: int) -> dict | None:
    """The printer behind one port of the bridge, or None when nothing answers there."""
    host = settings.usb_bridge_host
    uri = f"http://{host}:{port}/ipp/print"  # how it is asked; the queue uses the ipp:// form of the same address
    req = codec.make_request(codec.GET_PRINTER_ATTRIBUTES, 1, printer_uri=uri)
    req.group(codec.TAG_OPERATION).attrs.append(codec.Attr("requested-attributes", [
        codec.Val(codec.TAG_KEYWORD, a) for a in ("printer-make-and-model", "printer-info", "printer-device-id", "printer-uuid")]))
    async with sem:
        try:
            r = await client.post(uri, content=codec.encode(req), headers={"Content-Type": "application/ipp"})
            msg = codec.decode(r.content)
        except (httpx.HTTPError, codec.IppError, ValueError):
            return None
    g = next((g for g in msg.groups if g.tag == codec.TAG_PRINTER), None)
    if g is None or msg.code >= 0x100:
        return None
    model = _first(g, "printer-make-and-model") or _first(g, "printer-info")
    return {"port": port, "uri": f"ipp://{host}:{port}/ipp/print", "model": str(model).strip(), "id": parse_device_id(_first(g, "printer-device-id"))}


async def bridge_printers() -> list[dict]:
    if not settings.usb_bridge_host:
        return []
    sem = asyncio.Semaphore(_PROBES)
    async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
        found = await asyncio.gather(*[_ask_bridge(client, sem, p) for p in bridge_ports()])
    return [f for f in found if f]


async def usb_devices(upstream: httpx.AsyncClient) -> list[dict] | None:
    """USB printers CUPS' own backend sees: [{uri, model, id}]. None when CUPS could not say (then nothing is assumed)."""
    req = codec.make_request(CUPS_GET_DEVICES, 1, requesting_user_name=CUPS_ADMIN)
    ops = req.group(codec.TAG_OPERATION).attrs
    ops.append(codec.Attr("include-schemes", [codec.Val(codec.TAG_NAME, "usb")]))  # a comma separated NAME, as libcups sends it
    ops.append(codec.Attr("timeout", [codec.Val(codec.TAG_INTEGER, 8)]))
    ops.append(codec.Attr("requested-attributes", [codec.Val(codec.TAG_KEYWORD, a) for a in ("device-uri", "device-id", "device-make-and-model")]))
    try:
        r = await upstream.post("/", content=codec.encode(req), headers=IPP_HEADERS, timeout=20.0)
        msg = codec.decode(r.content)
    except (httpx.HTTPError, codec.IppError, ValueError):
        return None
    if msg.code >= 0x100:
        return None
    # CUPS lists every backend (network, cups-pdf, ...) when it does not honour include-schemes: keep only real USB devices
    return [{"uri": _first(g, "device-uri"), "model": str(_first(g, "device-make-and-model")).strip(),
             "id": parse_device_id(_first(g, "device-id"))}
            for g in msg.groups if g.tag == codec.TAG_PRINTER and urlsplit(str(_first(g, "device-uri"))).scheme == "usb"]


async def all_queues(upstream: httpx.AsyncClient) -> list[dict] | None:
    """Every CUPS queue with its device, stopped or not accepting jobs included: a queue that exists must never be created again
    (list_queues leaves out the ones not accepting jobs, which is what a printer that was just unplugged looks like)."""
    req = codec.make_request(0x4002, 1, requesting_user_name="gateway")
    req.group(codec.TAG_OPERATION).attrs.append(codec.Attr("requested-attributes", [codec.Val(codec.TAG_KEYWORD, a) for a in ("printer-name", "device-uri")]))
    try:
        r = await upstream.post("/", content=codec.encode(req), headers=IPP_HEADERS)
        msg = codec.decode(r.content)
    except (httpx.HTTPError, codec.IppError, ValueError):
        return None
    return [{"name": _first(g, "printer-name"), "device_uri": _first(g, "device-uri")} for g in msg.groups if g.tag == codec.TAG_PRINTER]


async def find_ppd(upstream: httpx.AsyncClient, ident: dict[str, str], model: str) -> str | None:
    """The driver (PPD) CUPS has for a device: the one whose IEEE 1284 model is exactly the device's, else whose name starts like it."""
    mfg, mdl = ident.get("mfg", ""), ident.get("mdl", "")
    if not mdl:
        return None
    req = codec.make_request(CUPS_GET_PPDS, 1, requesting_user_name="gateway")
    ops = req.group(codec.TAG_OPERATION).attrs
    ops.append(codec.Attr("requested-attributes", [codec.Val(codec.TAG_KEYWORD, a) for a in ("ppd-name", "ppd-device-id", "ppd-make-and-model")]))
    if mfg:
        ops.append(codec.Attr("ppd-make", [codec.Val(codec.TAG_TEXT, mfg)]))
    try:
        r = await upstream.post("/", content=codec.encode(req), headers=IPP_HEADERS, timeout=30.0)
        msg = codec.decode(r.content)
    except (httpx.HTTPError, codec.IppError, ValueError):
        return None
    loose = None
    for g in msg.groups:
        if g.tag != codec.TAG_PRINTER:
            continue
        name = _first(g, "ppd-name")
        if not name:
            continue
        if parse_device_id(_first(g, "ppd-device-id")).get("mdl", "").lower() == mdl.lower():
            return name
        mm = str(_first(g, "ppd-make-and-model")).lower()
        if loose is None and re.match(rf"{re.escape(f'{mfg} {mdl}'.lower())}(?![0-9a-z-])", mm):
            loose = name
    return loose


# --- doing ----------------------------------------------------------------------------------------------------------

async def _cups_admin(app, req: codec.IppMessage) -> bool:
    try:
        r = await app.state.upstream.post("/admin/", content=codec.encode(req), headers=IPP_HEADERS)
        ok = r.headers.get("content-type", "").startswith("application/ipp") and codec.decode(r.content).code < 0x100
    except (httpx.HTTPError, codec.IppError):
        log.exception("CUPS admin request failed")
        return False
    app.state.printers_cache = None
    app.state.queues_cache = None
    app.state.queue_caps_cache = None
    return ok


async def driver_for(upstream: httpx.AsyncClient, device_uri: str, virtual_ppd: str) -> str | None:
    """The driver to give a queue made again on `device_uri`: IPP printers are driverless ('everywhere'), a USB device gets the
    PPD CUPS has for it, a CUPS-PDF queue its own. None for anything else (a socket or lpd queue's driver can not be known)."""
    u = urlsplit(device_uri)
    if u.scheme in ("ipp", "ipps", "http", "https"):
        return "everywhere"
    if u.scheme == "cups-pdf":
        return virtual_ppd
    if u.scheme == "usb":
        for d in await usb_devices(upstream) or []:
            if d["uri"] == device_uri:
                return await find_ppd(upstream, d["id"], d["model"])
    return None


async def add_queue(app, name: str, device_uri: str, info: str, ppd_name: str, location: str = "") -> bool:
    req = codec.make_request(CUPS_ADD_MODIFY_PRINTER, 1, printer_uri=f"ipp://localhost/printers/{name}", requesting_user_name=CUPS_ADMIN)
    req.group(codec.TAG_OPERATION).attrs.append(codec.Attr("ppd-name", [codec.Val(codec.TAG_NAME, ppd_name)]))
    req.groups.append(codec.Group(codec.TAG_PRINTER, [
        codec.Attr("device-uri", [codec.Val(codec.TAG_URI, device_uri)]),
        codec.Attr("printer-is-accepting-jobs", [codec.Val(codec.TAG_BOOLEAN, True)]),
        codec.Attr("printer-state", [codec.Val(codec.TAG_ENUM, 3)]),
        codec.Attr("printer-info", [codec.Val(codec.TAG_TEXT, info)]),
    ] + ([codec.Attr("printer-location", [codec.Val(codec.TAG_TEXT, location)])] if location else [])))
    return await _cups_admin(app, req)


async def delete_queue(app, name: str) -> bool:
    return await _cups_admin(app, codec.make_request(CUPS_DELETE_PRINTER, 1, printer_uri=f"ipp://localhost/printers/{name}",
                                                     requesting_user_name=CUPS_ADMIN))


FORMER_REASONS = ("deleted", "removed_offline", "renamed")
FORMER_KEEP = 200  # names remembered; the oldest go first


def note_former(db, name: str, reason: str) -> None:
    """Remember that a queue used to have this name: the rename dialog offers it, so a replacement printer can take over the
    old address (and the permissions that name has in the lists)."""
    if reason not in FORMER_REASONS:
        raise ValueError(reason)
    key = ordering.key(name)
    row = db.get(PrinterFormer, key)
    now = datetime.now(UTC)
    if row is None:
        db.add(PrinterFormer(name=key, display=name.strip()[:128], removed_at=now, reason=reason))
    else:
        row.display, row.removed_at, row.reason = name.strip()[:128], now, reason
    db.flush()
    extra = db.scalars(select(PrinterFormer).order_by(PrinterFormer.removed_at.desc()).offset(FORMER_KEEP)).all()
    for old in extra:
        db.delete(old)


def took_name(db, name: str) -> None:
    """A queue has this name again (a rename, or a new queue): it is no longer a name of the past."""
    row = db.get(PrinterFormer, ordering.key(name))
    if row is not None:
        db.delete(row)


def forget_former(db, name: str) -> bool:
    """The administrator no longer wants this name offered in the rename dialog. True if it was there."""
    row = db.get(PrinterFormer, ordering.key(name))
    if row is None:
        return False
    db.delete(row)
    return True


def former_names(db, current: list[str]) -> list[PrinterFormer]:
    """The names of the past that no queue has now, most recent first (at most 20), for the rename dialog."""
    taken = {ordering.key(n) for n in current}
    rows = db.scalars(select(PrinterFormer).order_by(PrinterFormer.removed_at.desc())).all()
    return [r for r in rows if r.name not in taken][:20]


def forget_queue(db, name: str, reason: str) -> None:
    """Everything the gateway remembers about a queue that no longer exists (its settings, its place in the list); the name
    itself is kept as a name of the past (`reason`: deleted, removed_offline). Permissions that name a printer keep the name,
    so a replacement given the same name finds them again."""
    for model in (PrinterControl, PrinterKeepalive, PrinterOffline):
        row = db.get(model, ordering.key(name))
        if row is not None:
            db.delete(row)
    ordering.forget(db, name)  # also takes the queue out of every administrator's list
    note_former(db, name, reason)


def _audit(sm, action: str, name: str, **detail) -> None:
    with sm() as db:
        record(db, None, action, "printer", None, actor_name="system", printer=name, **detail)
        db.commit()


def _created(sm, name: str, **detail) -> None:
    """A queue was made by discovery: audit it, and its name stops being a name of the past."""
    _audit(sm, "printer.auto_add", name, **detail)
    with sm() as db:
        took_name(db, name)
        db.commit()


def _norm_uri(uri: str) -> tuple:
    """The same device address however it is spelled: ipp/ipps/http/https on one host, port and path are one printer."""
    u = urlsplit(uri)
    web = u.scheme in ("ipp", "ipps", "http", "https")
    return ("ipp" if web else u.scheme, (u.hostname or "").lower(), u.port or ({"ipp": 631, "http": 80, "ipps": 443, "https": 443}.get(u.scheme) if web else None), u.path)


# --- one pass -------------------------------------------------------------------------------------------------------

RETRY_REFUSED = timedelta(minutes=5)


def _cooling(app, uri: str) -> bool:
    """A device CUPS refused a moment ago is not asked again at once (every pass would fill CUPS' log with the same error)."""
    t = getattr(app.state, "discovery_refused", {}).get(_norm_uri(uri))
    return t is not None and datetime.now(UTC) - t < RETRY_REFUSED


def _refused(app, uri: str, ok: bool) -> None:
    d = getattr(app.state, "discovery_refused", None)
    if d is None:
        d = app.state.discovery_refused = {}
    if ok:
        d.pop(_norm_uri(uri), None)
    else:
        d[_norm_uri(uri)] = datetime.now(UTC)


async def _create_from_bridge(app, bridge: list[dict], have: set, taken: set, notes: list[dict]) -> int:
    """1. ipp-usb: driverless, preferred. Returns how many queues were made."""
    created = 0
    for p in bridge:
        if _norm_uri(p["uri"]) in have:
            continue
        if _cooling(app, p["uri"]):
            notes.append({"kind": "refused", "model": p["model"] or p["uri"]})  # still unresolved: keep saying so
            continue
        name = queue_name(p["model"], taken)
        made = await add_queue(app, name, p["uri"], p["model"] or name, "everywhere")
        _refused(app, p["uri"], made)
        if made:
            taken.add(ordering.key(name))
            created += 1
            log.info("new USB printer %r on the ipp-usb bridge: queue %s created", p["model"], name)
            await run_in_threadpool(_created, app.state.sessionmaker, name, model=p["model"], via="ipp-usb")
        else:
            notes.append({"kind": "refused", "model": p["model"] or p["uri"]})
    return created


async def _create_from_usb(app, devices: list[dict] | None, bridge: list[dict], have: set, served: set, taken: set,
                           notes: list[dict], streak: dict) -> int:
    """2. CUPS' own usb backend, only for what ipp-usb does not serve. Returns how many queues were made."""
    upstream = app.state.upstream
    created = 0
    for d in devices or []:
        mdl = d["id"].get("mdl", "").lower()
        if _norm_uri(d["uri"]) in have or (d["id"].get("mfg", "").lower(), mdl) in served:
            continue
        if any(mdl and mdl in p["model"].lower() for p in bridge):
            continue
        app.state.usb_unserved[d["uri"]] = streak.get(d["uri"], 0) + 1
        if app.state.usb_unserved[d["uri"]] < 2:
            continue
        model = d["model"] or d["uri"]
        ppd = await find_ppd(upstream, d["id"], model)
        if ppd is None:
            notes.append({"kind": "no_driver", "model": model})
            continue
        if _cooling(app, d["uri"]):
            notes.append({"kind": "refused", "model": model})
            continue
        name = queue_name(model, taken)
        made = await add_queue(app, name, d["uri"], model, ppd)
        _refused(app, d["uri"], made)
        if made:
            taken.add(ordering.key(name))
            created += 1
            log.info("new USB printer %r (no IPP-over-USB): queue %s created with %s", model, name, ppd)
            await run_in_threadpool(_created, app.state.sessionmaker, name, model=model, via="usb", driver=ppd)
        else:
            notes.append({"kind": "refused", "model": model})
    return created


async def _create_new(app, notes: list[dict]) -> int:
    upstream = app.state.upstream
    queues = await all_queues(upstream)
    if queues is None:  # CUPS did not say what exists: creating anything now could duplicate a queue
        return 0
    have = {_norm_uri(q["device_uri"]) for q in queues}
    taken = {ordering.key(q["name"]) for q in queues}
    bridge = await bridge_printers()
    served = {(p["id"].get("mfg", "").lower(), p["id"].get("mdl", "").lower()) for p in bridge if p["id"].get("mdl")}
    created = await _create_from_bridge(app, bridge, have, taken, notes)
    if not settings.usb_direct:
        return created
    devices = await usb_devices(upstream)
    app.state.usb_present = {d["uri"] for d in devices} if devices is not None else None
    # CUPS' usb backend also lists printers that ipp-usb serves. While ipp-usb restarts (it does, each time a USB device
    # is plugged or unplugged) such a printer is briefly not on the bridge: a device is taken directly only when it has
    # stayed unserved for two passes in a row, which is longer than the restart.
    streak = {u: n for u, n in getattr(app.state, "usb_unserved", {}).items()}
    app.state.usb_unserved = {}
    return created + await _create_from_usb(app, devices, bridge, have, served, taken, notes, streak)


def _track(sm, offline: set[str], hours_td: timedelta | None) -> list[str]:
    """Note since when each USB queue does not answer; return the ones that have been gone long enough to be removed."""
    now = datetime.now(UTC)
    with sm() as db:
        rows = {r.printer: r for r in db.scalars(select(PrinterOffline))}
        for name, row in rows.items():
            if name not in offline:  # back, or the queue is gone
                db.delete(row)
        for name in offline - rows.keys():
            db.add(PrinterOffline(printer=name, since=now))
        db.commit()
        if hours_td is None:
            return []
        return sorted(n for n in offline if n in rows and aware(rows[n].since) + hours_td <= now)


def offline_since(sm) -> dict[str, datetime]:
    with sm() as db:
        return {r.printer: aware(r.since) for r in db.scalars(select(PrinterOffline))}


async def run_once(app) -> dict:
    """One look at the USB bus and the queues. Returns {'created': n, 'removed': [names], 'notes': [...]}."""
    lock = getattr(app.state, "discovery_lock", None)
    if lock is None:  # one pass at a time (the timer and the "look now" button)
        lock = app.state.discovery_lock = asyncio.Lock()
    notes: list[dict] = []
    out = {"created": 0, "removed": [], "notes": notes}
    async with lock:
        sm = app.state.sessionmaker
        if settings.auto_discover:
            app.state.queues_cache = None
            out["created"] = await _create_new(app, notes)
        app.state.queues_cache = None
        queues = await list_queues(app)
        usb = [q for q in queues if is_usb_queue(q["device_uri"])]
        offline = {ordering.key(q["name"]) for q in usb if not q.get("online", True)}
        expired = await run_in_threadpool(_track, sm, offline, remove_after())
        for q in usb:
            if ordering.key(q["name"]) in expired:
                if await delete_queue(app, q["name"]):
                    def gone(name=q["name"]):
                        with sm() as db:
                            forget_queue(db, name, "removed_offline")
                            record(db, None, "printer.auto_remove", "printer", None, actor_name="system", printer=name,
                                   hours=settings.offline_remove_hours)
                            db.commit()
                    await run_in_threadpool(gone)
                    out["removed"].append(q["name"])
                    log.info("queue %s removed: its USB device did not answer for %s h", q["name"], settings.offline_remove_hours)
        if out["removed"]:
            app.state.queues_cache = None
    notes[:] = [n for i, n in enumerate(notes) if n not in notes[:i]]
    app.state.discovery = {"at": datetime.now(UTC), "notes": notes, **{k: out[k] for k in ("created", "removed")}}
    return out


async def run_scheduler(app) -> None:
    await asyncio.sleep(15)
    while True:
        try:
            await run_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("printer discovery failed")
        await asyncio.sleep(max(5.0, settings.discovery_interval))
