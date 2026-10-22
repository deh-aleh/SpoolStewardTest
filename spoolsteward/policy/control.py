"""Administrator switches: maintenance mode for the whole gateway, block / hide a single printer.

Two different things on purpose:
  * a *blocked* printer takes no job from anybody, administrators included (it is out of order or reserved);
  * *maintenance* stops printing and scanning for everybody except administrators and withdraws every mDNS
    announcement, so the printers disappear from phones and computers too.
"""

import hashlib
import hmac
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from spoolsteward.auth.compare import same_text
from spoolsteward.config import settings
from spoolsteward.db.models import GatewaySetting, PrinterControl, User
from spoolsteward.i18n import N_

MAINTENANCE = "maintenance"
STORE_TITLES = "store_job_titles"   # "0" = the audit does not keep the names of new files
BLOCKED_REASON = N_("printer disabled by the administrator (applies to everyone, administrators included)")
MAINTENANCE_REASON = N_("gateway in maintenance: only administrators can print or scan")


def key(printer: str) -> str:
    return printer.strip().lower()


def is_maintenance(db: Session) -> bool:
    row = db.get(GatewaySetting, MAINTENANCE)
    return bool(row and row.value == "1")


def set_maintenance(db: Session, on: bool) -> None:
    row = db.get(GatewaySetting, MAINTENANCE)
    if row is None:
        db.add(GatewaySetting(key=MAINTENANCE, value="1" if on else "0"))
    else:
        row.value = "1" if on else "0"


def titles_enabled(db: Session) -> bool:
    """Whether the names of the printed files are written to the audit (on unless an administrator turned it off)."""
    row = db.get(GatewaySetting, STORE_TITLES)
    return not (row and row.value == "0")


def set_titles(db: Session, on: bool) -> None:
    row = db.get(GatewaySetting, STORE_TITLES)
    if row is None:
        db.add(GatewaySetting(key=STORE_TITLES, value="1" if on else "0"))
    else:
        row.value = "1" if on else "0"


def controls(db: Session) -> dict[str, PrinterControl]:
    return {c.printer: c for c in db.scalars(select(PrinterControl))}


def set_printer_flag(db: Session, printer: str, field: str, value: bool) -> None:
    if field not in ("blocked", "mdns_hidden"):
        raise ValueError(field)
    row = db.get(PrinterControl, key(printer))
    if row is None:
        row = PrinterControl(printer=key(printer), blocked=False, mdns_hidden=False)
        db.add(row)
    setattr(row, field, value)
    row.updated_at = datetime.now(UTC)


def block_reason(db: Session, user: User | None, printer: str | None) -> str | None:
    """Why this submission must be refused, or None. For a scan `printer` is the queue on the scanner's device."""
    if is_maintenance(db) and not (user is not None and user.is_admin):
        return MAINTENANCE_REASON
    if printer:
        row = db.get(PrinterControl, key(printer))
        if row is not None and row.blocked:
            return BLOCKED_REASON
    return None


def mdns_policy(db: Session) -> dict:
    """What the mDNS announcer must (not) publish."""
    rows = controls(db)
    return {"maintenance": is_maintenance(db), "hidden": sorted(n for n, c in rows.items() if c.mdns_hidden)}


def mdns_token() -> str:
    """Shared secret between the gateway and the announcer (both read the same SPOOLSTEWARD_SECRET_KEY)."""
    return hmac.new((settings.secret_key or "").encode(), b"mdns-policy", hashlib.sha256).hexdigest()


def mdns_token_ok(given: str) -> bool:
    """Without SPOOLSTEWARD_SECRET_KEY nobody can prove who they are: the answer is always no (the announcer then announces
    nothing and logs how to fix it, see spoolsteward/mdns.py)."""
    return bool(settings.secret_key) and same_text(given or "", mdns_token())
