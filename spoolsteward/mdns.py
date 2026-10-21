"""DNS-SD (Bonjour/Avahi) announcement of the gateway's queues, so phones and desktops find them without typing anything.

Runs as its own process (`python -m spoolsteward.mdns`) in a container with host networking: multicast does not cross a
bridge network. It only reads the queue list from CUPS; the announced service is the gateway's TLS port.
Services are `_ipps._tcp` (+ the `_universal` subtype AirPrint looks for). The TXT records say
"username,password" is needed (air=), which is what the gateway demands for printing.
"""

import asyncio
import json
import logging
import re
import socket
from urllib.parse import urlparse

import httpx
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from spoolsteward.config import settings
from spoolsteward.cups_client import list_queues
from spoolsteward.logsetup import setup_logging
from spoolsteward.main import make_upstream

log = logging.getLogger("spoolsteward.mdns")
SERVICE = "_ipps._tcp.local."
UNIVERSAL = "_universal._sub._ipps._tcp.local."  # AirPrint looks for this subtype; plain-type browsers need SERVICE
BASE_FORMATS = ["application/pdf", "image/jpeg", "image/urf", "image/pwg-raster"]


def txt_for(q: dict, host: str, port: int, portal: str) -> dict[str, str]:
    """TXT record for one queue (RFC 6763 + IPP Everywhere / AirPrint keys). Values are capped at 255 bytes."""
    model = q.get("model") or q["name"]
    formats = [f for f in BASE_FORMATS if not q.get("formats") or f in q["formats"]] or ["application/pdf"]
    txt = {
        "txtvers": "1", "qtotal": "1",
        "rp": f"printers/{q['name']}",
        "ty": model, "product": f"({model})", "note": q.get("location") or "",
        "adminurl": portal,
        "pdl": ",".join(formats),
        "Color": "T" if q.get("color") else "F",
        "Duplex": "T" if q.get("duplex") else "F",
        "Copies": "T", "Scan": "F", "Fax": "F",
        "TLS": "1.2",
        "air": "username,password",
        "kind": "document,envelope,photo",
        "priority": "50",
        "URF": ",".join(q["urf"]) if q.get("urf") else "none",
    }
    if q.get("uuid"):
        txt["UUID"] = q["uuid"].removeprefix("urn:uuid:")
    return {k: v[:255] for k, v in txt.items()}


SCAN_SERVICE = "_uscans._tcp.local."  # eSCL over TLS


async def scanner_info(port: int, root: str = "eSCL") -> dict | None:
    """A scanner behind the gateway, read from its (anonymous) capabilities page on this machine."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=8.0) as c:
            r = await c.get(f"https://127.0.0.1:{port}/{root}/ScannerCapabilities")
        if r.status_code != 200:
            return None
    except httpx.HTTPError:
        return None
    text = r.text

    def tag(name: str) -> str:
        m = re.search(rf"<(?:\w+:)?{name}>([^<]*)<", text)
        return m.group(1).strip() if m else ""

    return {"model": tag("MakeAndModel") or "Scanner", "uuid": tag("UUID"), "root": root,
            "adf": "<scan:Adf>" in text or "<scan:Adf " in text, "color": "RGB24" in text}


def scanner_txt(info: dict, portal: str) -> dict[str, str]:
    txt = {
        "txtvers": "1", "ty": info["model"], "rs": info.get("root", "eSCL"), "representation": "",
        "pdl": "application/pdf,image/jpeg", "cs": "color,grayscale,binary" if info["color"] else "grayscale,binary",
        "is": "platen,adf" if info["adf"] else "platen", "duplex": "F", "adminurl": portal, "note": "", "air": "username,password",
    }
    if info.get("uuid"):
        txt["UUID"] = info["uuid"]
    return txt


async def fetch_policy(port: int, announce: dict | None = None) -> dict | None:
    """Which printers the administrators want hidden / whether the gateway is in maintenance, asked to the gateway
    itself (this container has no database). None = could not ask: the caller keeps what it knew."""
    from spoolsteward.policy.control import mdns_token

    try:
        async with httpx.AsyncClient(verify=False, timeout=8.0) as c:
            headers = {"X-Mdns-Token": mdns_token()}
            if announce:  # what is announced, so the gateway can warn when the TLS certificate does not cover it
                headers["X-Mdns-Announce"] = json.dumps(announce)
            r = await c.get(f"https://127.0.0.1:{port}/mdns/policy", headers=headers)
        if r.status_code != 200:
            return None
        data = r.json()
        return {"maintenance": bool(data.get("maintenance")), "hidden": {str(n).lower() for n in data.get("hidden", [])},
                "offline": {str(n).lower() for n in data.get("offline", [])}, "scanners": list(data.get("scanners", []))}
    except (httpx.HTTPError, ValueError):
        return None


def apply_policy(queues: list[dict], policy: dict | None) -> list[dict]:
    """Queues to announce: none in maintenance, never the ones an administrator hid, and not the ones whose device
    does not answer (a queue outlives the cable)."""
    if not policy:
        return queues
    if policy["maintenance"]:
        return []
    return [q for q in queues if q["name"].lower() not in policy["hidden"] | policy.get("offline", set())]


def default_address() -> list[str]:
    """IPv4 of the interface that has the default route (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))
        return [s.getsockname()[0]]


def _label(q: dict) -> str:
    return (q.get("info") or q["name"]).replace(".", " ")[:60]


def build_services(queues: list[dict], host: str, addresses: list[str], port: int, portal: str) -> dict[str, list[ServiceInfo]]:
    """service name -> [ServiceInfo for the plain type and the AirPrint subtype]. The SRV target is `<label>.local`:
    Android's system mDNS resolver (used by its built-in print service) only resolves ".local" names by multicast;
    a name like printers.lan would go to unicast DNS and the printer would never show up. Clients connect by the
    announced address, so the TLS certificate must list the IP (or the .local name)."""
    out = {}
    for q in queues:
        name = f"{_label(q)} @ {host.split('.')[0]}"
        props = txt_for(q, host, port, portal)
        out[name] = [ServiceInfo(t, f"{name}.{SERVICE}", port=port, properties=props, server=f"{host.split('.')[0]}.local.",
                                 addresses=[socket.inet_aton(a) for a in addresses])
                     for t in (SERVICE, UNIVERSAL)]
    return out


def _announce_settings() -> tuple[str, int, list[str], str]:
    """(host, port, addresses, portal URL) to announce, from the settings."""
    portal = settings.portal_url.rstrip("/")
    host = settings.mdns_hostname or (urlparse(portal).hostname if portal else "") or socket.gethostname()
    port = settings.mdns_port or settings.port
    addresses = [a.strip() for a in settings.mdns_addresses.split(",") if a.strip()] or default_address()
    if not portal:
        portal = f"https://{host}:{port}/me"
    elif not portal.endswith("/me"):
        portal += "/me"
    return host, port, addresses, portal


async def _scanner_services(policy: dict | None, host: str, addresses: list[str], port: int, portal: str) -> dict[str, list[ServiceInfo]]:
    """One service per scanner the gateway knows (none in maintenance); without a policy, the default one as before."""
    out = {}
    for sc in ([] if policy and policy["maintenance"] else (policy["scanners"] if policy else [{"root": "eSCL"}])):
        info = await scanner_info(port, sc["root"])
        if info:
            label = sc.get("name") or info["model"]
            name = f"{label} (scanner) @ {host.split('.')[0]}"
            out[name] = [ServiceInfo(SCAN_SERVICE, f"{name}.{SCAN_SERVICE}", port=port, properties=scanner_txt(info, portal),
                                     server=f"{host.split('.')[0]}.local.", addresses=[socket.inet_aton(a) for a in addresses])]
    return out


async def _publish(zcs: list[AsyncZeroconf], published: dict[str, list[ServiceInfo]], wanted: dict[str, list[ServiceInfo]]) -> None:
    """Bring what is announced (`published`, updated in place) in line with `wanted`: withdraw, update, announce."""
    for name in set(published) - set(wanted):
        for zc, info in zip(zcs, published.pop(name)):
            await zc.async_unregister_service(info)
        log.info("withdrawn: %s", name)
    for name, infos in wanted.items():
        if name in published:
            if all(a.properties == b.properties for a, b in zip(published[name], infos)):
                continue
            for zc, info in zip(zcs, infos):
                await zc.async_update_service(info)
            published[name] = infos
            log.info("updated: %s", name)
        else:
            for zc, info in zip(zcs, infos):
                await zc.async_register_service(info, allow_name_change=False, cooperating_responders=True)
            published[name] = infos
            log.info("announced: %s", name)


async def main() -> None:
    setup_logging(settings.log_level, settings.timezone)
    host, port, addresses, portal = _announce_settings()

    class _App:  # list_queues only needs app.state.upstream / queues_cache
        class state:  # noqa: N801
            upstream = make_upstream()
            queues_cache = None

    # one responder per record type: python-zeroconf keys its registry by instance name, so one instance cannot
    # publish the same printer under both the plain type and the AirPrint subtype
    zcs = [AsyncZeroconf(ip_version=IPVersion.V4Only, interfaces=addresses) for _ in (SERVICE, UNIVERSAL)]
    published: dict[str, list[ServiceInfo]] = {}
    policy: dict | None = None
    announce = {"names": [host, f"{host.split('.')[0]}.local"], "addresses": addresses}
    log.info("announcing as %s at %s:%s", host, addresses, port)
    try:
        while True:
            _App.state.queues_cache = None
            queues = await list_queues(_App, probe=False)  # the gateway tells which devices are unreachable
            policy = await fetch_policy(port, announce) or policy  # gateway unreachable: keep the last known policy
            wanted = build_services(apply_policy(queues, policy), host, addresses, port, portal)
            wanted.update(await _scanner_services(policy, host, addresses, port, portal))
            await _publish(zcs, published, wanted)
            await asyncio.sleep(settings.mdns_interval)
    finally:
        for zc in zcs:
            await zc.async_unregister_all_services()
            await zc.async_close()


if __name__ == "__main__":
    asyncio.run(main())
