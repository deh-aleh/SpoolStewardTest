"""Warnings about the TLS certificate, shown as notices in the portal.

The gateway never changes the certificate itself: `scripts/cert-sync.sh` (run by cron on the host) copies a renewed one
into config/tls and restarts the gateway, and writes the outcome to config/tls/sync-status.json. This module only looks
and tells the administrators when something is wrong:

    cert_sync_failed   the last run of cert-sync.sh failed (the message of the script is shown)
    cert_expired       the certificate has expired: devices refuse to connect          (always on)
    cert_expiring      it expires within SPOOLSTEWARD_CERT_EXPIRY_WARN_DAYS days             (off unless SPOOLSTEWARD_CERT_EXPIRY_WARN_ENABLED)
    cert_name_missing  none of the addresses/names announced on the network (mDNS) is in the certificate
                       (SPOOLSTEWARD_CERT_NAME_CHECK=false turns it off)

A notice closes by itself when the cause is gone.
"""

import asyncio
import ipaddress
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from starlette.concurrency import run_in_threadpool

from spoolsteward import notices
from spoolsteward.config import settings

log = logging.getLogger("spoolsteward.certwatch")
KINDS = ("cert_sync_failed", "cert_expired", "cert_expiring", "cert_name_missing")
KEY = "tls"
CHECK_EVERY = 6 * 3600


@dataclass
class CertInfo:
    not_after: datetime
    names: list[str]          # DNS names, lower case
    addresses: list[str]      # IP addresses


def read_cert(path: str | Path) -> CertInfo | None:
    """The first certificate of the file (the server's own one, in a fullchain.pem). None when it can not be read."""
    try:
        cert = x509.load_pem_x509_certificate(Path(path).read_bytes())
    except (OSError, ValueError):
        return None
    names, addresses = [], []
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [n.lower() for n in san.get_values_for_type(x509.DNSName)]
        addresses = [str(a) for a in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        pass
    if not names and not addresses:  # very old certificates only have the common name
        names = [a.value.lower() for a in cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)]
    return CertInfo(cert.not_valid_after_utc, names, addresses)


def covers(info: CertInfo, name: str) -> bool:
    """Is this address or name in the certificate? A wildcard (*.example.com) covers exactly one label."""
    name = name.strip().lower().rstrip(".")
    try:
        return str(ipaddress.ip_address(name)) in info.addresses
    except ValueError:
        pass
    for have in info.names:
        if have == name or (have.startswith("*.") and "." in name and name.split(".", 1)[1] == have[2:]):
            return True
    return False


def parse_announce(raw: str) -> dict | None:
    """What the announcer says it announces ({"names": [...], "addresses": [...]}), checked: it comes from a header."""
    try:
        data = json.loads(raw)
        names = [str(n)[:255] for n in data["names"]][:8]
        addresses = [str(ipaddress.ip_address(a)) for a in data["addresses"]][:8]
    except (ValueError, KeyError, TypeError):
        return None
    return {"names": names, "addresses": addresses}


def sync_status(cert_path: str | Path) -> dict | None:
    """The outcome file of scripts/cert-sync.sh, next to the certificate. None = the script never ran (or is not used)."""
    try:
        data = json.loads((Path(cert_path).parent / "sync-status.json").read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def evaluate(info: CertInfo | None, status: dict | None, announce: dict | None, now: datetime) -> dict[str, dict | None]:
    """kind -> details when that warning applies, None when it does not."""
    out: dict[str, dict | None] = dict.fromkeys(KINDS)
    if status and status.get("ok") is False:
        out["cert_sync_failed"] = {"message": str(status.get("message") or "")[:300], "when": status.get("ts")}
    if info is None:
        return out
    days = (info.not_after - now).days
    when = info.not_after.isoformat()
    if info.not_after <= now:
        out["cert_expired"] = {"not_after": when}
    elif settings.cert_expiry_warn_enabled and days < max(settings.cert_expiry_warn_days, 0):
        sync = "none" if status is None else ("failed" if status.get("ok") is False else "ok")
        out["cert_expiring"] = {"days": days, "not_after": when, "sync": sync}
    if settings.cert_name_check and announce:
        wanted = announce["addresses"] + announce["names"]
        if wanted and not any(covers(info, n) for n in wanted):
            out["cert_name_missing"] = {"addresses": announce["addresses"], "names": announce["names"]}
    return out


def apply(sm, results: dict[str, dict | None]) -> None:
    for kind, detail in results.items():
        if detail is None:
            notices.resolve(sm, kind, KEY)
        else:
            notices.report(sm, kind, KEY, detail)


def check_once(app, now: datetime | None = None) -> dict[str, dict | None]:
    if not settings.tls_certfile:
        return {}
    announce = getattr(app.state, "mdns_announce", None)
    if announce is None and settings.mdns_addresses.strip():  # the announcer has not called yet: use what is configured
        host = settings.mdns_hostname or urlparse(settings.portal_url).hostname or ""
        announce = {"addresses": [a.strip() for a in settings.mdns_addresses.split(",") if a.strip()],
                    "names": [host] if host else []}
    results = evaluate(read_cert(settings.tls_certfile), sync_status(settings.tls_certfile), announce,
                       now or datetime.now(UTC))
    apply(app.state.sessionmaker, results)
    return results


async def run_checker(app) -> None:
    await asyncio.sleep(90)  # let the announcer report first
    while True:
        try:
            await run_in_threadpool(check_once, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("certificate check failed")
        await asyncio.sleep(CHECK_EVERY)
