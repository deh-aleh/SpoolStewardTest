"""eSCL (AirScan) scanning through the gateway.

ipp-usb serves eSCL on the same port as IPP, so one device does both without fighting over the USB interface
(ipp-usb is the only owner; nothing else, SANE included, may claim it). The gateway authenticates the user with the
same personal keys as printing, records every scan in the audit (kind=scan) and applies scan limits if any exist.

Scanners are not configured one by one: they are found at run time. Every CUPS queue that lives on the network
(ipp-usb gives each USB printer its own port) is asked for its eSCL capabilities, and so is every address listed in
SPOOLSTEWARD_SCAN_URL ("url" or "name=url", comma separated). What answers is a scanner; what stops answering (cable
pulled, device off) disappears from the list within half a minute. The first one is the default for native clients
at /eSCL/...; every other is reachable at /eSCL-<id>/... and announced under that path.

Clients: native eSCL clients (they must be able to send HTTP Basic credentials) and the portal page /me/scan,
which needs nothing installed.
"""

import asyncio
import io
import logging
import re
import secrets
import socket
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urlsplit
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from spoolsteward import notices
from spoolsteward.timeutil import local_tz
from spoolsteward.admin.common import back, current_user, flash, render, verify_csrf
from spoolsteward.auth import service
from spoolsteward.auth.throttle import IP_FAILS, USER_FAILS, register_failure
from spoolsteward.config import settings
from spoolsteward.db.models import Group, JobKind, JobLog, JobState, User
from spoolsteward.cups_client import list_queues
from spoolsteward.ipp.gateway import _parse_basic, _unauthorized
from spoolsteward.policy import audit, control
from spoolsteward.i18n import N_, _
from spoolsteward.escl_window import Windows, normalize_ip
from spoolsteward.netutil import client_ip
from spoolsteward import i18n
from spoolsteward.admin.docs import slugify

log = logging.getLogger("spoolsteward.escl")
router = APIRouter()
me_router = APIRouter(prefix="/me")

ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
OPEN_PATHS = {"ScannerCapabilities", "ScannerStatus"}  # discovery: answered without credentials
MAX_SETTINGS = 64 << 10
COLORS = {"color": "RGB24", "gray": "Grayscale8", "bw": "BlackAndWhite1"}
FORMATS = {"pdf": "application/pdf", "jpeg": "image/jpeg"}
SOURCES = {"platen": "Platen", "adf": "Feeder"}  # plus "auto" in the portal
MAX_PAGES = 100
FALLBACK_DPI = (75, 100, 150, 200, 300)

JOB_FOLLOW_SECONDS = 1800.0  # a scan started without credentials can be followed by its address this long (a feeder can take minutes)
SCANNERS_TTL = 30.0
CAPS_TTL = 120.0
A4 = (2480, 3507)  # ThreeHundredthsOfInches: what a scanner is asked for when it does not tell its own maximum
PAPER = {"A4": (2480, 3508), "A3": (3508, 4961), "A5": (1748, 2480), "Letter": (2550, 3300), "Legal": (2550, 4200)}

SETTINGS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<scan:ScanSettings xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm" '
    'xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"><pwg:Version>2.0</pwg:Version>'
    "<pwg:ScanRegions><pwg:ScanRegion><pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>"
    "<pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset><pwg:Width>{w}</pwg:Width><pwg:Height>{h}</pwg:Height>"
    "</pwg:ScanRegion></pwg:ScanRegions><pwg:InputSource>{source}</pwg:InputSource>"
    "<scan:ColorMode>{color}</scan:ColorMode><scan:XResolution>{dpi}</scan:XResolution>"
    "<scan:YResolution>{dpi}</scan:YResolution><pwg:DocumentFormat>{fmt}</pwg:DocumentFormat></scan:ScanSettings>"
)


def make_client() -> httpx.AsyncClient:
    """One client for every scanner (absolute URLs): which devices exist is decided at run time, see `scanners`."""
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=settings.scan_timeout))


@dataclass(frozen=True)
class Scanner:
    id: str            # URL-safe and stable: the print queue on the same device, else "scanner"
    name: str          # what people see
    base: str          # http://host:port
    queue: str | None  # print queue on the same device (blocking it blocks the scanner too)
    root: str = "eSCL"  # path under which the gateway serves it


def _explicit() -> list[tuple[str | None, str]]:
    out = []
    for item in settings.scan_url.split(","):
        item = item.strip()
        if item:
            name, sep, url = item.partition("=") if not item.startswith(("http://", "https://")) else (None, "", item)
            out.append(((name.strip() or None) if sep else None, (url if sep else item).strip().rstrip("/")))
    return out


def _key(base: str) -> tuple[str, int]:
    u = urlsplit(base)
    return (u.hostname or "", u.port or (443 if u.scheme == "https" else 80))


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.]+", "-", text).strip("-")[:48] or "scanner"


async def _probe(client: httpx.AsyncClient, base: str) -> str | None:
    """MakeAndModel (possibly empty) when `base` speaks eSCL, None when it does not answer or is not a scanner."""
    try:
        r = await client.get(base + "/eSCL/ScannerCapabilities", timeout=3.0)
    except httpx.HTTPError:
        return None
    if r.status_code != 200 or "ScannerCapabilities" not in r.text:
        return None
    m = re.search(r"<(?:\w+:)?MakeAndModel>([^<]*)<", r.text)
    return m.group(1).strip() if m else ""


async def scanners(app) -> dict[str, Scanner]:
    """The scanners that answer right now, default first. Looked up every SCANNERS_TTL seconds."""
    client: httpx.AsyncClient | None = getattr(app.state, "scan", None)
    if client is None:
        return {}
    cached = getattr(app.state, "scanners_cache", None)
    if cached and cached[0] == id(client) and time.monotonic() - cached[1] < SCANNERS_TTL:
        return cached[2]
    cands = await _candidates(app, client)
    found = await asyncio.gather(*[_probe(client, base) for _, base, _ in cands.values()])
    busy = {r.get("base") for r in getattr(app.state, "web_scans", {}).values() if r.get("state") == "running"}
    previous = cached[2] if cached and cached[0] == id(client) else {}
    ordered = _with_paths(_name_scanners(cands, found, previous, busy))
    app.state.scanners_cache = (id(client), time.monotonic(), ordered)
    return ordered


async def _candidates(app, client: httpx.AsyncClient) -> dict[tuple[str, int], tuple[str | None, str, str | None]]:
    """Where a scanner might be: the configured ones, the client's own address, and the network printers' addresses."""
    cands: dict[tuple[str, int], tuple[str | None, str, str | None]] = {}  # key -> (label, base, queue)
    for label, base in _explicit():
        cands.setdefault(_key(base), (label, base, None))
    if str(client.base_url):  # a client built around one scanner (tests, embedding)
        cands.setdefault(_key(str(client.base_url)), (None, str(client.base_url).rstrip("/"), None))
    for q in await list_queues(app):
        u = urlsplit(q.get("device_uri", ""))
        if u.scheme not in ("ipp", "ipps", "http", "https") or not u.hostname or u.hostname in ("localhost", "127.0.0.1", "::1"):
            continue
        if not q.get("online", True):
            continue
        for port in dict.fromkeys((u.port or 631, 80)):  # the IPP port (ipp-usb serves eSCL there) or plain web
            base = f"http://{u.hostname}:{port}"
            prev = cands.get(_key(base))
            cands[_key(base)] = (prev[0] if prev else None, base, (prev[2] if prev else None) or q["name"])
    return cands


def _name_scanners(cands: dict, found: list, previous: dict[str, Scanner], busy: set) -> dict[str, Scanner]:
    """The candidates that answered, each with a name and a unique id."""
    out: dict[str, Scanner] = {}
    claimed: set[str] = set()  # one scanner per print queue, even if it answers on two ports
    for (label, base, queue), model in zip(cands.values(), found):
        if model is not None and queue and queue in claimed:
            continue
        if model is not None and queue:
            claimed.add(queue)
        if model is None:
            old = next((x for x in previous.values() if x.base == base), None)
            if old is not None and base in busy:  # a scanner in the middle of a scan may not answer: keep it
                out[old.id] = old
            continue
        name = label or queue or model or "Scanner"
        sid, n = _slug(queue or name), 2
        while sid in out:
            sid, n = f"{_slug(queue or name)}-{n}", n + 1
        out[sid] = Scanner(sid, name, base, queue)
    return out


def _with_paths(out: dict[str, Scanner]) -> dict[str, Scanner]:
    ordered = {}
    for i, (sid, sc) in enumerate(out.items()):  # the first is the default and keeps the historic /eSCL path
        ordered[sid] = Scanner(sc.id, sc.name, sc.base, sc.queue, "eSCL" if i == 0 else f"eSCL-{sc.id}")
    return ordered


async def default_scanner(app) -> Scanner | None:
    return next(iter((await scanners(app)).values()), None)


async def scanner_device(app) -> str | None:
    """Name of the device scans are shown under for rows that only say "scanner" (recorded before the audit
    kept the queue name): the default scanner's queue."""
    cached = getattr(app.state, "scanner_device_cache", None)
    if cached and time.monotonic() - cached[0] < 60.0:
        return cached[1]
    sc = await default_scanner(app)
    name = (sc.queue or sc.name) if sc else None
    app.state.scanner_device_cache = (time.monotonic(), name)
    return name


def _public(request: Request) -> str:
    scheme = "https" if (settings.public_scheme or request.url.scheme) in ("https", "ipps") else "http"
    return f"{scheme}://{request.headers.get('host', 'localhost')}"


# ---- audit helpers (sync, run in the threadpool)

def _start(sm, user: User, title: str, options: dict, scanner: Scanner | None = None):
    """Reserve the scan in the audit under the scanner's device name (so a blocked printer blocks its scanner too)."""
    options = {**options, "scanner": scanner.name} if scanner else options
    with sm() as s:
        return audit.reserve(s, user, (scanner.queue or "scanner") if scanner else "scanner", title, options, local_tz(), kind=JobKind.scan)


def _mark(sm, log_id: int, state: JobState, pages: int | None = None, reason: str | None = None) -> None:
    with sm() as s:
        row = s.get(JobLog, log_id)
        if row is None or row.state in (JobState.denied, JobState.failed, JobState.canceled):
            return
        if state is JobState.canceled and row.state is not JobState.accepted:
            return  # a scan that already delivered its page stays a completed scan
        if state is JobState.completed and pages:
            row.pages = (row.pages or 0) + pages
        row.state = state
        if reason:
            row.denied_reason = reason
        s.commit()


async def _hostname(ip: str) -> str:
    """Name the DNS gives to the address, if any, within a moment (a slow resolver must not hold up the scan)."""
    try:
        return (await asyncio.wait_for(run_in_threadpool(socket.gethostbyaddr, ip), 1.5))[0]
    except (OSError, asyncio.TimeoutError):
        return ""


def _guest(sm) -> User | None:
    """The shared identity of credential-less scans (SPOOLSTEWARD_ESCL_GUEST_USERNAME): a local account with no password,
    so nobody can log in as it. If an account with that name already exists and is a real one (a password, RADIUS, an
    administrator), it is NEVER used: the scan would be credited to a real person. Returns None, tells the administrators
    (bell, rate-limited, and the log) and the caller refuses the scan."""
    name = settings.escl_guest_username
    with sm() as s:
        user = s.scalars(select(User).where(User.username == name)).first()
        if user is None:
            group = s.scalars(select(Group).where(Group.name == settings.default_group)).first()
            user = User(provider="local", username=name, enabled=True, is_admin=False, group_id=group.id if group else None)
            s.add(user)
            s.commit()
        elif user.provider != "local" or user.password_hash or user.is_admin:
            taken = {"provider": user.provider, "admin": bool(user.is_admin), "has_password": bool(user.password_hash)}
            s.rollback()
            notices.report(sm, "escl_guest_name_taken", name, taken)
            return None
        s.expunge(user)
    notices.resolve(sm, "escl_guest_name_taken", name)   # the problem is gone (another name, or the account was removed)
    return user


def _jobs(request: Request) -> dict:
    return request.app.state.__dict__.setdefault("scan_jobs", {})  # "<root>/<scan job id>" -> (log_id, user_id, scanner base, address that started it, monotonic start)


def _windows(app) -> Windows:
    w = getattr(app.state, "scan_windows", None)
    if w is None:
        w = app.state.scan_windows = Windows()
    return w


def _enabled_user(sm, user_id: int) -> User | None:
    with sm() as s:
        user = s.get(User, user_id)
        if user is None or not user.enabled:
            return None
        s.expunge(user)
        return user


async def _without_credentials(request: Request, parts: list[str], root: str, peer: str) -> tuple[User | None, bool]:
    """Who a request with no credentials is, if the scan window says so: (user, True), or (None, False).
    1. A scan job that started from this address goes on for the person it belongs to, even after the window ended
       (a window that closes mid-scan must not cut the scan off: the page of the scan would never arrive).
    2. Otherwise an open window for this address makes the request the window owner's."""
    if not settings.escl_window:
        return None, False
    sm = request.app.state.sessionmaker
    if len(parts) >= 2 and parts[0] == "ScanJobs":
        owner = _jobs(request).get(f"{root}/{parts[1]}")
        if owner is not None and len(owner) > 4 and owner[3] == peer and time.monotonic() - owner[4] < JOB_FOLLOW_SECONDS:
            user = await run_in_threadpool(_enabled_user, sm, owner[1])
            return user, user is not None
    win = _windows(request.app).for_ip(peer)
    if win is None:
        return None, False
    user = await run_in_threadpool(_enabled_user, sm, win.user_id)
    return user, user is not None


def _report_mismatch(app, got: str) -> None:
    """A request without credentials came from `got` while windows are open for other addresses: write the refusal in the
    audit (for the person who opened it) and in their bell, with both addresses. Rate limited per pair."""
    sm = app.state.sessionmaker
    windows = _windows(app)
    for w in windows.others(got):
        if not windows.mismatch_due(w.ip, got):
            continue
        reason = f"scan window is for {w.ip}, request from {got}"
        with sm() as s:
            s.add(JobLog(ts=audit.now_utc(), user_id=w.user_id, user_name=w.username, kind=JobKind.scan, printer="scanner",
                         title="scan", state=JobState.denied, denied_reason=reason,
                         options_json={"via": "escl", "window_ip": w.ip, "got_ip": got}))
            s.commit()
        notices.notify_user_counted(sm, w.user_id, "scan_window_mismatch", key=f"{w.ip}|{got}",
                                    detail={"expected": w.ip, "got": got})
        log.warning("scan refused: window of %s is for %s, request from %s", w.username, w.ip, got)


# ---- native eSCL clients

@router.api_route("/eSCL/{path:path}", methods=["GET", "POST", "DELETE"], include_in_schema=False)
async def escl_default(path: str, request: Request) -> Response:
    return await _escl(request, path, None)


@router.api_route("/eSCL-{sid}/{path:path}", methods=["GET", "POST", "DELETE"], include_in_schema=False)
async def escl_named(sid: str, path: str, request: Request) -> Response:
    return await _escl(request, path, sid)


@dataclass
class _Who:
    """Who a scan request is: the account, and how it was recognised."""

    user: User | None
    peer: str
    window_used: bool = False   # recognised by the scan window of their address
    is_guest: bool = False      # the shared guest identity


def _shape_ok(request: Request, path: str, parts: list[str]) -> bool:
    return (
        (request.method == "GET" and path in OPEN_PATHS) or
        (request.method == "POST" and path == "ScanJobs") or
        (len(parts) == 3 and parts[0] == "ScanJobs" and request.method == "GET" and parts[2] in ("NextDocument", "ScanImageInfo")) or
        (len(parts) == 2 and parts[0] == "ScanJobs" and request.method == "DELETE")
    ) and (len(parts) < 2 or bool(ID_RE.match(parts[1])))


async def _escl(request: Request, path: str, sid: str | None) -> Response:
    scan: httpx.AsyncClient | None = getattr(request.app.state, "scan", None)
    parts = path.split("/")
    if scan is None or not _shape_ok(request, path, parts):
        return Response("not found", status_code=404, media_type="text/plain")
    if request.url.scheme != "https" and not settings.allow_insecure_auth:
        return Response("TLS required", status_code=403, media_type="text/plain")
    registry = await scanners(request.app)
    scanner = registry.get(sid) if sid else next(iter(registry.values()), None)
    root = "eSCL" if sid is None else f"eSCL-{sid}"
    if scanner is None and (path in OPEN_PATHS or path == "ScanJobs"):
        return Response("not found", status_code=404, media_type="text/plain")  # nothing answers there (any more)

    who = await _who_is_asking(request, path, parts, root)
    if isinstance(who, Response):
        return who
    sm = request.app.state.sessionmaker
    try:
        if request.method == "GET" and path in OPEN_PATHS:
            r = await scan.get(scanner.base + "/eSCL/" + path)
            return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "text/xml"))
        if request.method == "POST":  # new scan job
            return await _new_scan_job(request, scan, scanner, root, sm, who)
        return await _scan_job_request(request, scan, parts, root, sm, who.user)
    except httpx.HTTPError as exc:
        log.error("scanner unreachable: %s", exc)
        return Response("scanner unreachable", status_code=502, media_type="text/plain")


async def _who_is_asking(request: Request, path: str, parts: list[str], root: str) -> _Who | Response:
    """The account behind a scan request: the scan window, the guest identity, or a login. A Response = refused.
    (The open discovery paths need nobody: then `user` is None.)"""
    user = None
    creds = _parse_basic(request.headers.get("authorization"))
    sm = request.app.state.sessionmaker
    peer = normalize_ip(request.client.host if request.client else None) or client_ip(request)
    window_used = is_guest = False
    if creds is None:
        user, window_used = await _without_credentials(request, parts, root, peer)
    if user is not None:
        pass
    elif creds is None and settings.escl_anonymous and path not in OPEN_PATHS:
        # many scan apps (NAPS2, ...) cannot send credentials: opt-in guest identity, audited with the client IP
        user = await run_in_threadpool(_guest, sm)
        if user is None:
            return _unauthorized()
        is_guest = True
    elif not (path in OPEN_PATHS and settings.anonymous_discovery):
        if creds is None:
            if path not in OPEN_PATHS and not settings.escl_anonymous:  # an app that sends no credentials: tell the administrators once in a while
                await run_in_threadpool(notices.report, sm, "escl_anonymous_refused", peer, {"path": path})
            if settings.escl_window:  # somebody is waiting for a scan from another address: tell them what went wrong
                await run_in_threadpool(_report_mismatch, request.app, peer)
            return _unauthorized()
        user = await _login(request, creds, peer)
        if isinstance(user, Response):
            return user
    return _Who(user, peer, window_used, is_guest)


async def _login(request: Request, creds: tuple[str, str], ip: str) -> User | Response:
    """Basic credentials of a scan request: throttle, password or print key."""
    throttle = request.app.state.throttle
    ukey, ikey = f"u:{creds[0].strip().lower()}|{ip}", f"ip:{ip}"
    if throttle.blocked(ukey, USER_FAILS) or throttle.blocked(ikey, IP_FAILS):
        return Response("too many failed attempts", status_code=429, headers={"Retry-After": "60"})
    user = await service.print_login(request.app, creds[0], creds[1], ip)
    if user is None:
        register_failure(throttle, ukey, ikey, creds[1])
        log.warning("scan auth failed user=%r ip=%s", creds[0], ip)
        return _unauthorized("invalid credentials")
    throttle.clear(ukey)
    return user


async def _new_scan_job(request: Request, scan: httpx.AsyncClient, scanner: Scanner, root: str, sm, who: _Who) -> Response:
    """POST ScanJobs: reserve the scan in the audit, start it on the scanner, remember who owns it."""
    user, peer = who.user, who.peer
    body = await request.body()
    if len(body) > MAX_SETTINGS:
        return Response("too large", status_code=413, media_type="text/plain")
    via = {"via": "escl"}
    if who.window_used:
        via.update(window_ip=peer, window_host=await _hostname(peer))
    elif who.is_guest:
        via.update(guest_ip=peer, guest_host=await _hostname(peer))
    if b"Feeder" in body:  # the client asked for the document feeder (ADF)
        via["source"] = "adf"
    decision, log_id = await run_in_threadpool(_start, sm, user, "scan", via, scanner)
    if not decision.allowed:
        log.info("scan denied user=%s reason=%s", user.username, decision.reason)
        return Response(decision.reason, status_code=503, media_type="text/plain")
    r = await scan.post(scanner.base + "/eSCL/ScanJobs", content=body, headers={"Content-Type": request.headers.get("content-type", "text/xml")})
    loc = r.headers.get("location", "")
    job = loc.rstrip("/").rsplit("/", 1)[-1]
    if r.status_code != 201 or not ID_RE.match(job):
        await run_in_threadpool(_mark, sm, log_id, JobState.failed, None, f"scanner status {r.status_code}")
        return Response(r.content, status_code=r.status_code if r.status_code != 201 else 502,
                        media_type=r.headers.get("content-type", "text/plain"))
    jobs = _jobs(request)
    while len(jobs) >= 256:
        jobs.pop(next(iter(jobs)))  # forgotten jobs never pile up
    jobs[f"{root}/{job}"] = (log_id, user.id, scanner.base, peer if who.window_used else None, time.monotonic())  # a window scan: that address may finish it on its own
    log.info("scan job %s on %s user=%s", job, scanner.name, user.username)
    return Response(status_code=201, headers={"Location": f"{_public(request)}/{root}/ScanJobs/{job}"})


async def _scan_job_request(request: Request, scan: httpx.AsyncClient, parts: list[str], root: str, sm, user: User) -> Response:
    """DELETE of a scan job, or its NextDocument / ScanImageInfo: only the owner (or an administrator) may."""
    job = parts[1]
    owner = _jobs(request).get(f"{root}/{job}")
    if owner is None or (owner[1] != user.id and not user.is_admin):
        return Response("not found", status_code=404, media_type="text/plain")
    log_id, base = owner[0], owner[2]  # the scanner that took the job, even if it left the list meanwhile

    if request.method == "DELETE":
        r = await scan.delete(f"{base}/eSCL/ScanJobs/{job}")
        await run_in_threadpool(_mark, sm, log_id, JobState.canceled, None, "canceled by the client")
        _jobs(request).pop(f"{root}/{job}", None)
        return Response(status_code=r.status_code)

    # NextDocument / ScanImageInfo: stream the page back and count it
    upstream_req = scan.build_request("GET", f"{base}/eSCL/ScanJobs/{job}/{parts[2]}")
    r = await scan.send(upstream_req, stream=True)
    headers = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "content-length")}
    if r.status_code == 200 and parts[2] == "NextDocument":
        await run_in_threadpool(_mark, sm, log_id, JobState.completed, 1)

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            if r.is_stream_consumed:  # already read (small answers, test transports)
                yield r.content
            else:
                async for chunk in r.aiter_raw():
                    yield chunk
        finally:
            await r.aclose()
    return StreamingResponse(body_iter(), status_code=r.status_code, headers=headers)


# ---- portal: scan from the browser

def paper_label(w: int, h: int) -> str:
    """Name of the paper size a scan area (in 1/300 inch) corresponds to."""
    for name, (pw, ph) in PAPER.items():
        if abs(w - pw) <= 40 and abs(h - ph) <= 60:
            return name
    return f"{round(w * 25.4 / 300)}×{round(h * 25.4 / 300)} mm"


def parse_caps(xml: str) -> dict:
    """Per input source: what the scanner really offers (resolutions, formats, largest area, double-sided feeder)."""
    out: dict = {}
    for key, tag in (("platen", "PlatenInputCaps"), ("adf", "AdfSimplexInputCaps")):
        m = re.search(rf"<scan:{tag}>(.*?)</scan:{tag}>", xml, re.S)
        if not m:
            continue
        sec = m.group(1)
        dpi = sorted({int(x) for x in re.findall(r"<scan:XResolution>(\d+)<", sec)})
        caps = {"dpi": dpi or list(FALLBACK_DPI),
                "formats": sorted(set(re.findall(r"<pwg:DocumentFormat>([^<]+)<", sec)) - {"application/octet-stream"})}
        w, h = re.search(r"<scan:MaxWidth>(\d+)<", sec), re.search(r"<scan:MaxHeight>(\d+)<", sec)
        if w and h:
            caps.update(w=int(w.group(1)), h=int(h.group(1)), size=paper_label(int(w.group(1)), int(h.group(1))))
        if key == "adf":
            caps["duplex"] = "<scan:AdfDuplexInputCaps" in xml
        out[key] = caps
    return out


async def scanner_caps(app, scanner: Scanner) -> dict:
    """What this scanner can do, read from the device itself (cached a couple of minutes)."""
    client: httpx.AsyncClient | None = getattr(app.state, "scan", None)
    store = getattr(app.state, "scan_caps", None)
    if not isinstance(store, dict) or store.get("_client") != id(client):
        store = app.state.scan_caps = {"_client": id(client)}
    hit = store.get(f"{scanner.id}|{scanner.base}")
    if hit and time.monotonic() - hit[0] < CAPS_TTL:
        return hit[1]
    caps: dict = {}
    if client is not None:
        try:
            r = await client.get(scanner.base + "/eSCL/ScannerCapabilities")
            if r.status_code == 200:
                caps = parse_caps(r.text)
        except httpx.HTTPError:
            pass
    caps = caps or {"platen": {"dpi": list(FALLBACK_DPI), "formats": []}}
    store[f"{scanner.id}|{scanner.base}"] = (time.monotonic(), caps)
    return caps


async def _adf_loaded(scan: httpx.AsyncClient, base: str) -> bool:
    try:
        r = await scan.get(base + "/eSCL/ScannerStatus")
        return r.status_code == 200 and "ScannerAdfLoaded" in r.text
    except httpx.HTTPError:
        return False


def _merge_pdf(pages: list[bytes]) -> bytes:
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for p in pages:
        for page in PdfReader(io.BytesIO(p)).pages:
            w.add_page(page)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


async def scan_once(app, user: User, scanner: Scanner, source: str, color: str, dpi: int, fmt: str) -> tuple[bytes, str, int, str]:
    """Run one scan job as `user` on `scanner`: (file, content-type, pages, extension). Raises RuntimeError with a
    readable message. source: platen | adf | auto (feeder when it holds paper, else glass). The feeder is read to its end."""
    scan: httpx.AsyncClient | None = getattr(app.state, "scan", None)
    if scan is None:
        raise RuntimeError(_("Scanning is not enabled on this server"))
    caps, source = await _check_scan_choice(app, scan, scanner, source, dpi)
    sm = app.state.sessionmaker
    decision, log_id = await run_in_threadpool(_start, sm, user, "scan", {"via": "web", "format": fmt, "dpi": dpi, "source": source}, scanner)
    if not decision.allowed:
        if decision.reason in (control.BLOCKED_REASON, control.MAINTENANCE_REASON):
            raise RuntimeError(decision.text())
        raise RuntimeError(_("%(reason)s. You can ask for more quota from your profile.", reason=decision.text()))
    area = (caps[source].get("w", A4[0]), caps[source].get("h", A4[1]))  # the whole sheet the scanner can read
    xml = SETTINGS_XML.format(source=SOURCES[source], color=COLORS[color], dpi=dpi, fmt=FORMATS[fmt], w=area[0], h=area[1])
    pages = await _run_scan_job(scan, scanner, source, xml, sm, log_id)
    return await _pack_pages(pages, fmt)


async def _check_scan_choice(app, scan: httpx.AsyncClient, scanner: Scanner, source: str, dpi: int) -> tuple[dict, str]:
    """What the scanner offers, and the source to use ("auto" = the feeder when it holds paper). RuntimeError if not possible."""
    caps = await scanner_caps(app, scanner)
    if source == "auto":
        source = "adf" if "adf" in caps and await _adf_loaded(scan, scanner.base) else "platen"
    if source not in caps:
        raise RuntimeError(_("This scanner has no automatic feeder") if source == "adf" else _("Source not available"))
    if dpi not in caps[source]["dpi"]:
        raise RuntimeError(_("With the feeder the maximum quality is %(n)s dpi", n=max(caps[source]["dpi"])) if source == "adf"
                           else _("With the glass the maximum quality is %(n)s dpi", n=max(caps[source]["dpi"])))
    return caps, source


async def _run_scan_job(scan: httpx.AsyncClient, scanner: Scanner, source: str, xml: str, sm, log_id: int) -> list[bytes]:
    """Start the job, read the pages, close the audit row (completed or failed) and always delete the job on the scanner."""
    job, pages = None, []
    try:
        for attempt in range(3):  # a sleeping scanner answers "busy" once while it wakes up
            r = await scan.post(scanner.base + "/eSCL/ScanJobs", content=xml, headers={"Content-Type": "text/xml"})
            if r.status_code not in (409, 503):
                break
            await asyncio.sleep(2.5)
        if r.status_code != 201:
            hint = (_("The feeder is empty or the scanner was paused: remove and reload the sheets, then try again.")
                    if source == "adf" else _("The scanner is busy: try again in a few seconds."))
            raise RuntimeError(_("The scanner refused the job (status %(code)s). %(hint)s", code=r.status_code, hint=hint))
        job = r.headers.get("location", "").rstrip("/").rsplit("/", 1)[-1]
        if not ID_RE.match(job):
            raise RuntimeError(_("Unexpected answer from the scanner"))
        while len(pages) < (MAX_PAGES if source == "adf" else 1):
            doc = await scan.get(f"{scanner.base}/eSCL/ScanJobs/{job}/NextDocument")
            if doc.status_code != 200 or not doc.content:
                break  # 404/410 = no more sheets
            pages.append(doc.content)
        if not pages:
            raise RuntimeError(_("The scanner delivered no page"))
        await run_in_threadpool(_mark, sm, log_id, JobState.completed, len(pages))
    except httpx.HTTPError as exc:
        await run_in_threadpool(_mark, sm, log_id, JobState.failed, None, "scanner unreachable")
        raise RuntimeError(_("The scanner does not answer")) from exc
    except RuntimeError as exc:
        await run_in_threadpool(_mark, sm, log_id, JobState.failed, None, str(exc)[:200])
        raise
    finally:
        if job and ID_RE.match(job):
            try:
                await scan.delete(f"{scanner.base}/eSCL/ScanJobs/{job}")
            except httpx.HTTPError:
                pass
    return pages


async def _pack_pages(pages: list[bytes], fmt: str) -> tuple[bytes, str, int, str]:
    """The scanned pages as one file: (file, content-type, pages, extension)."""
    if fmt == "pdf":
        data = pages[0] if len(pages) == 1 else await run_in_threadpool(_merge_pdf, pages)
        return data, "application/pdf", len(pages), "pdf"
    if len(pages) == 1:
        return pages[0], "image/jpeg", 1, "jpg"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, p in enumerate(pages, 1):
            z.writestr(f"pagina-{n:02d}.jpg", p)
    return buf.getvalue(), "application/zip", len(pages), "zip"


def clean_basename(raw: str, default: str) -> str:
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*')[:120].strip(" .")
    return name or default


# The scan runs as a background task of the server, not of the browser request: leaving the page (or a phone going to
# sleep) used to abort it half way and leave the scanner busy. The page polls; the result waits a few minutes.
RESULT_TTL = 900.0
_SCAN_LABELS = {"running": N_("in progress"), "done": N_("ready"), "error": N_("unsuccessful")}


def _default_scan_name() -> str:
    return _("scan-%(stamp)s", stamp=datetime.now(local_tz()).strftime("%Y%m%d-%H%M"))


def _scans(app) -> dict:
    d = getattr(app.state, "web_scans", None)
    if d is None:
        d = app.state.web_scans = {}
    now = time.monotonic()
    for sid in [k for k, v in d.items() if v["state"] != "running" and now - v["ts"] > RESULT_TTL]:
        d.pop(sid)
    return d


async def _run_scan(app, rec: dict, user: User, scanner: Scanner, source: str, color: str, dpi: int, fmt: str) -> None:
    try:
        rec["data"], rec["ctype"], rec["pages"], rec["ext"] = await scan_once(app, user, scanner, source, color, dpi, fmt)
        rec["state"] = "done"
    except RuntimeError as exc:
        rec["state"], rec["error"] = "error", str(exc)
    except Exception:
        log.exception("web scan crashed")
        rec["state"], rec["error"] = "error", _("Unexpected error during the scan")
    rec["ts"] = time.monotonic()


def _view(sid: str, rec: dict) -> dict:
    label = _SCAN_LABELS.get(rec["state"], rec["state"])
    return {"id": sid, "state": rec["state"], "label": _(label), "error": rec.get("error"),
            "pages": rec.get("pages"), "name": f'{rec["name"]}.{rec["ext"]}' if rec.get("ext") else rec["name"]}


@me_router.get("/scan", response_class=HTMLResponse)
async def scan_form(request: Request, user: User = Depends(current_user)):
    found = await scanners(request.app)
    caps = {sid: await scanner_caps(request.app, sc) for sid, sc in found.items()}
    mine = [_view(sid, r) for sid, r in reversed(list(_scans(request.app).items())) if r["user_id"] == user.id]
    return render(request, "scan.html", user, enabled=bool(found), found=list(found.values()), caps=caps,
                  default_name=_default_scan_name(), mine=mine,
                  window_on=settings.escl_window and bool(found), window_seconds=settings.escl_window_seconds,
                  window_left=_windows(request.app).remaining(user.id), window_doc=_window_doc_href())


# the section of the user guide about the scan window (the popup links to it); the headings are in docs/{en,it}/
WINDOW_DOC = {"en": ("user-guide", "Scanning from an app that cannot sign in"),
              "it": ("guida-utente", "Scansione da un'app che non sa accedere")}


def _window_doc_href() -> str:
    lang = "it" if i18n.current().startswith("it") else "en"
    page, heading = WINDOW_DOC[lang]
    return f"/docs/{lang}/{page}#{slugify(heading)}"


_PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")


@me_router.post("/scan/window", dependencies=[Depends(verify_csrf)])
async def window_open(request: Request, user: User = Depends(current_user)):
    """The button: from now on, scan requests without credentials that come from this address are this person's."""
    if not settings.escl_window or not await scanners(request.app):
        raise HTTPException(404)
    seen = normalize_ip(request.client.host if request.client else None)
    if not seen:
        return JSONResponse({"error": _("The server can not tell which address you are connecting from.")}, status_code=409)
    w = _windows(request.app).open(seen, user.id, user.username, settings.escl_window_seconds)
    if w is None:
        return JSONResponse({"error": _("Too many scan windows are open: try again in a few minutes.")}, status_code=503)
    log.info("scan window opened user=%s ip=%s seconds=%d", user.username, seen, settings.escl_window_seconds)
    return JSONResponse({"ip": seen, "seconds": settings.escl_window_seconds, "ipv6": ":" in seen,
                         "proxy": any(h in request.headers for h in _PROXY_HEADERS)},
                        headers={"Cache-Control": "no-store"})


@me_router.post("/scan/window/close", dependencies=[Depends(verify_csrf)])
async def window_close(request: Request, user: User = Depends(current_user)):
    _windows(request.app).close(user.id)  # a scan that already started goes on: it is tied to the address, not to the window
    return JSONResponse({"closed": True}, headers={"Cache-Control": "no-store"})


@me_router.post("/scan", dependencies=[Depends(verify_csrf)])
async def scan_submit(request: Request, source: str = Form("auto"), color: str = Form("color"),
                      dpi: int = Form(300), fmt: str = Form("pdf"), filename: str = Form(""), scanner: str = Form(""),
                      user: User = Depends(current_user)):
    if source not in (*SOURCES, "auto") or color not in COLORS or fmt not in FORMATS:
        raise HTTPException(400, "invalid scan settings")
    wants_json = request.headers.get("x-requested-with") == "fetch"
    found = await scanners(request.app)
    chosen = found.get(scanner) if scanner else next(iter(found.values()), None)
    if chosen is None:
        msg = _("The scanner “%(name)s” does not answer: refresh the page.", name=scanner) if scanner else _("No scanner available.")
        if wants_json:
            return JSONResponse({"error": msg}, status_code=409)
        flash(request, msg, "err")
        return back("/me/scan")
    scans = _scans(request.app)
    if any(r["state"] == "running" and r.get("scanner") == chosen.id for r in scans.values()):
        msg = _("%(name)s is already scanning: wait for it to finish.", name=chosen.name)
        if wants_json:
            return JSONResponse({"error": msg}, status_code=409)
        flash(request, msg, "err")
        return back("/me/scan")
    sid = secrets.token_urlsafe(12)
    name = clean_basename(filename, _default_scan_name())
    rec = scans[sid] = {"user_id": user.id, "state": "running", "name": name, "ts": time.monotonic(),
                        "scanner": chosen.id, "base": chosen.base}
    rec["task"] = asyncio.create_task(_run_scan(request.app, rec, user, chosen, source, color, dpi, fmt))
    if wants_json:
        return JSONResponse({"id": sid})
    return back("/me/scan")


def _mine(request: Request, sid: str, user: User) -> dict:
    rec = _scans(request.app).get(sid)
    if rec is None or rec["user_id"] != user.id:
        raise HTTPException(404)
    return rec


@me_router.get("/scan/{sid}")
async def scan_status(request: Request, sid: str, user: User = Depends(current_user)):
    return JSONResponse(_view(sid, _mine(request, sid, user)), headers={"Cache-Control": "no-store"})


@me_router.get("/scan/{sid}/file")
async def scan_file(request: Request, sid: str, user: User = Depends(current_user)):
    rec = _mine(request, sid, user)
    if rec["state"] != "done":
        raise HTTPException(404)
    name = f'{rec["name"]}.{rec["ext"]}'
    ascii_name = "".join(c if c.isascii() and c not in '"\\;' else "_" for c in name)
    return Response(rec["data"], media_type=rec["ctype"], headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
        "Cache-Control": "no-store"})
