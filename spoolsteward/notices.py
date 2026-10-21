"""Notices for administrators (the bell in the portal), with a limit on how often the same one is reported.

A notice is identified by its kind and its source (for example "escl_anonymous_refused" and an IP address). The first time
it happens it is written to the database and to the log. After that, repeats from the same source are only counted
in memory until the interval has passed, so a client that retries 100 times a minute produces one line a day, not 100.
The interval depends on SPOOLSTEWARD_LOG_LEVEL (a quieter log level means rarer reports) unless SPOOLSTEWARD_NOTICE_INTERVAL is set:

    DEBUG   every minute     (for people looking for a problem)
    INFO    once a day       (default)
    WARNING every 3 days
    ERROR   every 7 days
"""

import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from spoolsteward.timeutil import aware
from spoolsteward.config import settings
from spoolsteward.db.models import Notice

log = logging.getLogger("spoolsteward.notices")

BY_LEVEL = {"DEBUG": 60, "INFO": 24 * 3600, "WARNING": 3 * 24 * 3600, "ERROR": 7 * 24 * 3600, "CRITICAL": 7 * 24 * 3600}
MAX_OPEN_PER_KIND = 200   # sources are limited too: spoofed addresses must not fill the list
KEEP_DISMISSED = timedelta(days=90)
LOG_TEXT = {
    "escl_anonymous_refused": "scan request without credentials refused from %s (%d attempt(s) since the last report): "
                              "scans without login are off (SPOOLSTEWARD_ESCL_ANONYMOUS=false)",
    "escl_guest_name_taken": "scans without login refused: the account name '%s' (SPOOLSTEWARD_ESCL_GUEST_USERNAME) belongs to a real account "
                             "(%d refusal(s) since the last report). Set another name in .env and restart the portal",
}

_lock = threading.Lock()
_state: dict[tuple[str, str], dict] = {}   # (kind, source) -> {"last": unix time of the last report, "pending": repeats since}


def interval() -> int:
    if settings.notice_interval > 0:
        return settings.notice_interval
    return BY_LEVEL.get(settings.log_level.upper(), BY_LEVEL["INFO"])


def report(sm, kind: str, key: str, detail: dict | None = None, now: float | None = None) -> bool:
    """Count one more occurrence. Returns True when this one was written (and logged), False when it was only counted."""
    now = time.time() if now is None else now
    key = key[:128]
    wait = interval()
    with _lock:
        st = _state.get((kind, key))
        if st is None:  # after a restart: the database still knows when this source was last reported
            with sm() as db:
                last = db.scalar(select(func.max(Notice.last_at)).where(Notice.kind == kind, Notice.key == key, Notice.user_id.is_(None)))
            if last is not None and now - aware(last).timestamp() < wait:
                st = _state[(kind, key)] = {"last": aware(last).timestamp(), "pending": 0}
        if st is not None and now - st["last"] < wait:
            st["pending"] += 1
            return False
        pending = st["pending"] if st else 0
        _state[(kind, key)] = {"last": now, "pending": 0}
        if len(_state) > 2000:  # forget sources that are past their interval
            for k in [k for k, v in _state.items() if now - v["last"] >= wait]:
                del _state[k]
    when = datetime.fromtimestamp(now, UTC)
    with sm() as db:
        row = db.scalars(select(Notice).where(Notice.kind == kind, Notice.key == key, Notice.user_id.is_(None), Notice.dismissed_at.is_(None))).first()
        if row is not None:
            row.last_at, row.count, row.detail_json = when, row.count + pending + 1, detail or row.detail_json
        else:
            open_now = db.scalar(select(func.count(Notice.id)).where(Notice.kind == kind, Notice.user_id.is_(None), Notice.dismissed_at.is_(None))) or 0
            if open_now < MAX_OPEN_PER_KIND:
                db.add(Notice(kind=kind, key=key, first_at=when, last_at=when, count=pending + 1, detail_json=detail))
        for old in db.scalars(select(Notice).where(Notice.dismissed_at < when - KEEP_DISMISSED)):
            db.delete(old)
        db.commit()
    if kind in LOG_TEXT:
        log.warning(LOG_TEXT[kind], key, pending + 1)
    else:
        log.warning("notice %s from %s (%d)", kind, key, pending + 1)
    return True


def resolve(sm, kind: str, key: str) -> int:
    """Close the open notices of this source because the problem is gone (dismissed by "system"). Returns how many."""
    key = key[:128]
    with sm() as db:
        rows = list(db.scalars(select(Notice).where(Notice.kind == kind, Notice.key == key, Notice.user_id.is_(None), Notice.dismissed_at.is_(None))))
        for row in rows:
            row.dismissed_at, row.dismissed_by = datetime.now(UTC), "system"
        db.commit()
    if rows:
        with _lock:
            _state.pop((kind, key), None)  # if it happens again it is a new problem and is reported at once
    return len(rows)


def pending(kind: str, key: str) -> int:
    """Repeats counted in memory and not yet written (shown beside the stored count)."""
    with _lock:
        st = _state.get((kind, key))
        return st["pending"] if st else 0


MAX_OPEN_PER_USER = 100
# Notices about something that happened to one print, not to the account: they are marked as read by themselves after the
# time the person chose (User.secondary_read_minutes). Today: prints changed by the permissions (colour, one-sided...).
SECONDARY_KINDS = ("job_adjusted",)


def notify_user(sm, user_id: int, kind: str, detail: dict | None = None, key: str | None = None) -> None:
    """A notice for one user (their own bell). Every event is written, no rate limit: these are things an administrator
    just did to the user's account. The list per user is capped, the oldest open ones are closed first.
    `key`: what the notice is about (default: the key prefix of the detail), used to find it again."""
    when = datetime.now(UTC)
    with sm() as db:
        open_rows = list(db.scalars(select(Notice).where(Notice.user_id == user_id, Notice.dismissed_at.is_(None))
                                    .order_by(Notice.id)))
        for old in open_rows[:max(0, len(open_rows) - MAX_OPEN_PER_USER + 1)]:
            old.dismissed_at, old.dismissed_by = when, "system"
        db.add(Notice(kind=kind, key=(key if key is not None else str((detail or {}).get("prefix", "")))[:128], user_id=user_id, first_at=when, last_at=when,
                      count=1, detail_json=detail))
        for old in db.scalars(select(Notice).where(Notice.dismissed_at < when - KEEP_DISMISSED)):
            db.delete(old)
        db.commit()


def notify_user_counted(sm, user_id: int, kind: str, key: str, detail: dict | None = None) -> None:
    """One notice per event for the person, without filling the list: while an open notice of this kind and subject exists
    it is renewed (time, counter, detail) and shows as new again in the bell; once dismissed, the next event opens a new one."""
    key = key[:128]
    with sm() as db:
        row = db.scalars(select(Notice).where(Notice.user_id == user_id, Notice.kind == kind, Notice.key == key,
                                              Notice.dismissed_at.is_(None))).first()
        if row is not None:
            row.last_at, row.count, row.detail_json = datetime.now(UTC), row.count + 1, detail or row.detail_json
            db.commit()
            return
    notify_user(sm, user_id, kind, detail, key=key)


def read_secondary(sm, user_id: int, minutes: int, now: datetime | None = None) -> int:
    """Mark as read (dismissed by "auto") the secondary notices of this person whose last event is older than `minutes`.
    0 = never. Returns how many."""
    if minutes <= 0:
        return 0
    now = now or datetime.now(UTC)
    with sm() as db:
        rows = list(db.scalars(select(Notice).where(Notice.user_id == user_id, Notice.kind.in_(SECONDARY_KINDS),
                                                    Notice.dismissed_at.is_(None), Notice.last_at < now - timedelta(minutes=minutes))))
        for row in rows:
            row.dismissed_at, row.dismissed_by = now, "auto"
        if rows:
            db.commit()
    return len(rows)


def notify_all_users(sm, kind: str, detail: dict | None = None, key: str = "", exclude_user_id: int | None = None) -> int:
    """The same notice in the bell of every active account, administrators included (an administrator reads theirs in the
    administrators' page). `exclude_user_id`: the person who did it, who needs no telling. One write for everybody, and the
    per-user cap applies as in notify_user. Returns how many people were notified."""
    from spoolsteward.db.models import User

    when = datetime.now(UTC)
    with sm() as db:
        ids = [i for i in db.scalars(select(User.id).where(User.enabled.is_(True))) if i != exclude_user_id]
        for uid in ids:
            open_rows = list(db.scalars(select(Notice).where(Notice.user_id == uid, Notice.dismissed_at.is_(None)).order_by(Notice.id)))
            for old in open_rows[:max(0, len(open_rows) - MAX_OPEN_PER_USER + 1)]:
                old.dismissed_at, old.dismissed_by = when, "system"
            db.add(Notice(kind=kind, key=key[:128], user_id=uid, first_at=when, last_at=when, count=1, detail_json=detail))
        db.commit()
    return len(ids)


def notify_user_once(sm, user_id: int, kind: str, key: str, detail: dict | None = None) -> bool:
    """Like notify_user, but not again while an open notice of this kind and subject exists. True when one was written."""
    with sm() as db:
        if db.scalar(select(func.count(Notice.id)).where(Notice.user_id == user_id, Notice.kind == kind, Notice.key == key[:128],
                                                         Notice.dismissed_at.is_(None))):
            return False
    notify_user(sm, user_id, kind, detail, key=key)
    return True


def resolve_users(sm, kind: str, key: str) -> int:
    """Close the personal notices of this kind and subject (the problem they told about is gone). Returns how many."""
    with sm() as db:
        rows = list(db.scalars(select(Notice).where(Notice.user_id.is_not(None), Notice.kind == kind, Notice.key == key[:128],
                                                    Notice.dismissed_at.is_(None))))
        for row in rows:
            row.dismissed_at, row.dismissed_by = datetime.now(UTC), "system"
        db.commit()
    return len(rows)


def unseen_count(sm, user_id: int, own: bool = False) -> int:
    """Open notices that are new, or happened again, since this person last opened the list (the red dot).
    `own`: the notices addressed to this person; otherwise the administrators' ones."""
    from spoolsteward.db.models import User

    with sm() as db:
        seen = db.scalar(select(User.own_notices_seen_at if own else User.notices_seen_at).where(User.id == user_id))
        q = select(func.count(Notice.id)).where(Notice.dismissed_at.is_(None),
                                                Notice.user_id == user_id if own else Notice.user_id.is_(None))
        if seen is not None:
            q = q.where(Notice.last_at > seen)
        return db.scalar(q) or 0
