"""Small helpers that talk to CUPS on behalf of the gateway itself (not on behalf of a user)."""

import asyncio
import logging
import time
from urllib.parse import quote, urlsplit

import httpx

from starlette.concurrency import run_in_threadpool

from spoolsteward.ipp import codec
from spoolsteward.policy import ordering

log = logging.getLogger(__name__)
_TTL = 30.0
_ATTRS = ["printer-name", "printer-info", "printer-state", "printer-is-accepting-jobs", "printer-make-and-model",
          "printer-location", "printer-uuid", "document-format-supported", "urf-supported", "color-supported",
          "sides-supported", "media-supported", "media-default", "print-color-mode-supported", "device-uri"]

# paper sizes offered in the portal (IPP keyword -> label); only the ones a queue really lists are shown
MEDIA_LABELS = {
    "iso_a3_297x420mm": "A3", "iso_a4_210x297mm": "A4", "iso_a5_148x210mm": "A5", "iso_a6_105x148mm": "A6",
    "iso_b5_176x250mm": "B5", "na_letter_8.5x11in": "Letter", "na_legal_8.5x14in": "Legal",
    "na_executive_7.25x10.5in": "Executive", "na_5x7_5x7in": "13x18 cm (5x7\")", "na_index-4x6_4x6in": "10x15 cm (4x6\")",
}


_CAPS_TTL = 60.0
_CAPS_MAX = 500  # queue names come from request paths: the cache keeps successes only, and never grows past this
UNKNOWN_CAPS = {"color": None, "duplex": None}


async def queue_caps(app, name: str) -> dict:
    """What one queue can do, for the permission checks: {"color": bool|None, "duplex": bool|None} (None = CUPS did not
    say). One Get-Printer-Attributes for this queue only, cached for a minute (list_queues would probe every device on the
    network). A queue that is not there, or any error, gives "unknown": the caller then acts only on what a client asked."""
    upstream = getattr(app.state, "upstream", None)
    if upstream is None or not name:
        return dict(UNKNOWN_CAPS)
    key = ordering.key(name)
    cache = getattr(app.state, "queue_caps_cache", None)
    if cache is None:
        cache = app.state.queue_caps_cache = {}
    hit = cache.get(key)
    if hit and time.monotonic() - hit[0] < _CAPS_TTL:
        return dict(hit[1])
    req = codec.make_request(codec.GET_PRINTER_ATTRIBUTES, 1, printer_uri=f"ipp://localhost/printers/{quote(name, safe='')}",
                             requesting_user_name="gateway")
    req.group(codec.TAG_OPERATION).attrs.append(
        codec.Attr("requested-attributes", [codec.Val(codec.TAG_KEYWORD, a) for a in ("color-supported", "sides-supported")]))
    try:
        resp = await upstream.post(f"/printers/{quote(name, safe='')}", content=codec.encode(req),
                                   headers={"Content-Type": "application/ipp", "Host": "localhost"})
        msg = codec.decode(resp.content)
    except (httpx.HTTPError, codec.IppError) as exc:
        log.warning("could not read the capabilities of queue %s: %s", name, exc)
        return dict(UNKNOWN_CAPS)
    g = msg.group(codec.TAG_PRINTER)
    if msg.code >= 0x100 or g is None:
        return dict(UNKNOWN_CAPS)
    caps = {"color": bool(next(iter(_all(g, "color-supported")), False)),
            "duplex": any(v != "one-sided" for v in _all(g, "sides-supported"))}
    if len(cache) >= _CAPS_MAX:
        cache.clear()
    cache[key] = (time.monotonic(), caps)
    return dict(caps)


_PORTS = {"ipp": 631, "ipps": 631, "http": 80, "https": 443, "socket": 9100, "lpd": 515}


async def is_online(device_uri: str, usb_present: set[str] | None = None) -> bool:
    """Does anything answer at the printer's network address? A queue stays in CUPS after the cable is pulled, so
    its existence says nothing. Only network URIs can be checked (ipp-usb is one: unplugging closes its port);
    a `usb:` device is present when CUPS' usb backend listed it (`usb_present`, kept by the discovery task; unknown = assumed
    present); anything else (cups-pdf:, file:) is assumed present."""
    u = urlsplit(device_uri)
    if u.scheme == "usb" and usb_present is not None:
        return device_uri in usb_present
    if u.scheme not in _PORTS or not u.hostname or u.hostname in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(u.hostname, u.port or _PORTS[u.scheme]), 2.0)
        writer.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def _all(g, name: str) -> list:
    a = g.get(name)
    return [v.value for v in a.values] if a else []


async def list_queues(app, probe: bool = True) -> list[dict]:
    """Queues that accept jobs: [{name, info, model, online...}], cached for a few seconds. Physical printers come first,
    oldest connection first (see spoolsteward.policy.ordering).
    `probe`: also check that the device answers (needs to run where the printers' addresses resolve: the gateway)."""
    cached = getattr(app.state, "queues_cache", None)
    if cached and time.monotonic() - cached[0] < _TTL:
        return cached[1]
    upstream = getattr(app.state, "upstream", None)
    if upstream is None:
        return []
    req = codec.make_request(0x4002, 1, requesting_user_name="gateway")  # CUPS-Get-Printers
    ops = req.group(codec.TAG_OPERATION)
    ops.attrs.append(codec.Attr("requested-attributes", [codec.Val(codec.TAG_KEYWORD, a) for a in _ATTRS]))
    try:
        resp = await upstream.post("/", content=codec.encode(req),
                                             headers={"Content-Type": "application/ipp", "Host": "localhost"})
        msg = codec.decode(resp.content)
    except (httpx.HTTPError, codec.IppError) as exc:
        log.warning("could not list CUPS queues: %s", exc)
        return cached[1] if cached else []
    queues = []
    for g in msg.groups:
        if g.tag != codec.TAG_PRINTER:
            continue
        name, accepting = g.get("printer-name"), g.get("printer-is-accepting-jobs")
        if name and (accepting is None or accepting.first):
            info, model = g.get("printer-info"), g.get("printer-make-and-model")
            queues.append({"name": name.first, "info": (info.first if info else "") or "",
                           "model": (model.first if model else "") or "",
                           "location": next(iter(_all(g, "printer-location")), "") or "",
                           "uuid": next(iter(_all(g, "printer-uuid")), "") or "",
                           "device_uri": next(iter(_all(g, "device-uri")), "") or "",
                           "formats": _all(g, "document-format-supported"),
                           "urf": _all(g, "urf-supported"),
                           "color": bool(next(iter(_all(g, "color-supported")), False)),
                           "media": [{"id": m, "label": MEDIA_LABELS[m]} for m in _all(g, "media-supported") if m in MEDIA_LABELS],
                           "media_default": next(iter(_all(g, "media-default")), "") or "",
                           "color_modes": _all(g, "print-color-mode-supported"),
                           "duplex": any(v != "one-sided" for v in _all(g, "sides-supported"))})
    for q in queues:
        q["virtual"] = ordering.is_virtual(q["device_uri"])
    sm = getattr(app.state, "sessionmaker", None)  # the mDNS announcer has no database: plain alphabetical there
    seen = await run_in_threadpool(ordering.note_seen, sm, [q["name"] for q in queues]) if sm is not None else {}
    queues.sort(key=lambda q: ordering.sort_key(q["name"], q["device_uri"], seen))
    online = await asyncio.gather(*[is_online(q["device_uri"], getattr(app.state, "usb_present", None)) if probe else asyncio.sleep(0, True) for q in queues])
    for q, up in zip(queues, online):
        q["online"] = up
    app.state.queues_cache = (time.monotonic(), queues)
    return queues
