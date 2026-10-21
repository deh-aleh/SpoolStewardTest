"""Housekeeping once an hour: forget the print keys that are over, warn the owner of the ones about to expire, and tell
people whose temporary permission (granted after a request) has just ended."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app import notices
from app.auth import keys
from app.config import settings
from app.db.models import PrintKey
from app.policy import permissions
from app.timeutil import aware

log = logging.getLogger("app.keywatch")
EVERY = 3600


def warn_expiring(sm, now: datetime | None = None) -> int:
    """One notice per key, sent once, when it is within SPOOLSTEWARD_KEY_EXPIRY_WARN_DAYS of its end (extending the key re-arms it)."""
    now = now or datetime.now(UTC)
    soon = now + timedelta(days=max(settings.key_expiry_warn_days, 1))
    sent = 0
    with sm() as db:
        for k in db.scalars(select(PrintKey).where(PrintKey.expires_at.is_not(None), PrintKey.expiry_notified_at.is_(None),
                                                   PrintKey.single_use.is_(False), PrintKey.revoked_at.is_(None))):
            if keys.key_state(k, now) not in ("active", "disabled") or aware(k.expires_at) > soon:
                continue
            notices.notify_user(sm, k.user_id, "key_expiring", {"label": k.label, "prefix": k.key_prefix, "until": k.expires_at.isoformat()})
            k.expiry_notified_at = now
            sent += 1
        db.commit()
    return sent


def run_once(sm, now: datetime | None = None) -> tuple[int, int]:
    """(keys forgotten, key expiry warnings sent). Also tells the people whose temporary permission has ended."""
    with sm() as db:
        gone = keys.purge(db, now)
    sent = warn_expiring(sm, now) if settings.key_expiry_warn_enabled else 0
    ended = permissions.notify_expired(sm, now)
    if ended:
        log.info("%d temporary permission(s) ended: their owners were told", ended)
    return gone, sent


async def run_keys(app) -> None:
    await asyncio.sleep(30)
    while True:
        try:
            gone, sent = await run_in_threadpool(run_once, app.state.sessionmaker)
            if gone or sent:
                log.info("print keys: %d forgotten, %d owner(s) warned of an expiry", gone, sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("print key housekeeping failed")
        await asyncio.sleep(EVERY)
