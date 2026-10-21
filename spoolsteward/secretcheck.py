"""Checks on SPOOLSTEWARD_SECRET_KEY at every start, told to the administrators through the bell.

The key encrypts the file names kept in the audit (spoolsteward/crypto_fields.py), signs the portal sessions and the channel to the
mdns announcer. A backup without encryption does not contain it, so whoever loses `.env` loses the names for good.

  * no key set                -> notice "secret_key_missing" (rate-limited like every notice), closed once a key is set;
  * the key is not the one of the previous start (a fingerprint is kept, never the key)
                              -> notice "secret_key_changed" with how many names can no longer be read;
  * "save the key somewhere safe" -> told ONCE to the first-run administrator (or to all administrators when that
                                 account does not exist), and again only after the key changes.

What was already told is remembered in `gateway_setting`, not in the notices themselves: closed notices are deleted after
a while (notices.KEEP_DISMISSED), and the reminder must not come back at the next restart.
"""

import hashlib
import hmac
import logging
from datetime import UTC, datetime

from sqlalchemy import select, text

from spoolsteward import notices
from spoolsteward.config import settings
from spoolsteward.db.models import GatewaySetting, User

log = logging.getLogger("spoolsteward.secretcheck")

CHECK = "secret_key_check"     # fingerprint of the key of the previous start
TOLD = "secret_key_notice"     # when "save the key" was sent
_LABEL = b"spoolsteward-key-check-v1"


def fingerprint(secret: str) -> str:
    """Tells two keys apart without revealing them (an HMAC with the key itself as the key)."""
    return hmac.new(secret.encode(), _LABEL, hashlib.sha256).hexdigest()[:32]


def _get(db, key: str) -> str | None:
    row = db.get(GatewaySetting, key)
    return row.value if row is not None else None


def _put(db, key: str, value: str | None) -> None:
    row = db.get(GatewaySetting, key)
    if value is None:
        if row is not None:
            db.delete(row)
    elif row is None:
        db.add(GatewaySetting(key=key, value=value))
    else:
        row.value = value


def _sealed_names(db) -> int:
    """Rows whose file name is encrypted (read as text: decrypting them is what can no longer be done)."""
    return db.execute(text("SELECT COUNT(*) FROM job_log WHERE title LIKE 'enc1:%'")).scalar() or 0


def _first_admin(db) -> User | None:
    if not settings.bootstrap_admin:
        return None
    user = db.scalars(select(User).where(User.username == settings.bootstrap_admin_username)).first()
    return user if user is not None and user.provider == "local" and user.enabled and user.is_admin else None


def run(sm) -> None:
    """Called once at start. Never raises: a failure here must not stop the gateway."""
    try:
        _run(sm)
    except Exception:
        log.exception("check of SPOOLSTEWARD_SECRET_KEY failed")


def _run(sm) -> None:
    secret = settings.secret_key
    if not secret:
        log.warning("SPOOLSTEWARD_SECRET_KEY is not set: file names are not encrypted and sessions end at every restart")
        notices.report(sm, "secret_key_missing", "")
        return
    notices.resolve(sm, "secret_key_missing", "")
    fp = fingerprint(secret)
    changed = None
    with sm() as db:
        old = _get(db, CHECK)
        if old is not None and old != fp:
            changed = _sealed_names(db)
            _put(db, TOLD, None)  # the new key has to be saved too
        if old != fp:
            _put(db, CHECK, fp)
        db.commit()
    if changed is not None:
        log.warning("SPOOLSTEWARD_SECRET_KEY changed since the previous start: %d encrypted file name(s) can no longer be read", changed)
        notices.report(sm, "secret_key_changed", "", {"count": changed})
    with sm() as db:
        if _get(db, TOLD) is not None:
            return
        admin = _first_admin(db)
        admin_id = admin.id if admin is not None else None
        _put(db, TOLD, datetime.now(UTC).isoformat())
        db.commit()
    if admin_id is not None:
        notices.notify_user(sm, admin_id, "secret_key_save", key="secret_key")
    else:
        notices.report(sm, "secret_key_save", "")
