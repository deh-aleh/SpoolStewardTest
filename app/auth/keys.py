"""Personal print keys. A key is a random secret the user pastes into the OS print dialog instead of the
domain password. High entropy, so a fast hash (sha256) is the right tool: a slow one would only make every
one of the 3-4 requests of a print job expensive and open a CPU-exhaustion hole."""

import hashlib
import secrets
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.timeutil import aware
from app.config import settings
from app.db.models import PrintKey, User
from app.i18n import _

KEY_TAG = "cpk-"
MAX_LABEL = 64
MAX_KEYS_PER_USER = 20
TOUCH_EVERY = 60.0  # seconds: last_used is informational, not worth a DB write per request

# What the user can pick when creating a key (days), plus "one print". The longest one allowed depends on the account (ttl_days_for).
DURATIONS = (30, 90, 180, 365, 548, 730)
DEFAULT_DURATION = 365
ONCE = "once"

_last_touch: dict[int, float] = {}


def hash_key(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def create_key(session: Session, user: User, label: str, ttl_days: int | None, now: datetime | None = None,
               once: bool = False) -> tuple[PrintKey, str]:
    """Returns (row, plaintext). The plaintext exists only in this return value: show it once.
    `once`: valid for one print, and void after SPOOLSTEWARD_KEY_ONCE_HOURS if nobody uses it."""
    label = label.strip()
    if not label or len(label) > MAX_LABEL:
        raise ValueError(_("the label must be 1-%(n)s characters", n=MAX_LABEL))
    now = now or datetime.now(UTC)
    active = [k for k in session.scalars(select(PrintKey).where(PrintKey.user_id == user.id)) if key_state(k, now) in ("active", "disabled")]
    if len(active) >= MAX_KEYS_PER_USER:
        raise ValueError(_("at most %(n)s active keys per user: revoke some first", n=MAX_KEYS_PER_USER))
    plain = KEY_TAG + secrets.token_urlsafe(24)  # 192 bits
    row = PrintKey(
        user_id=user.id, label=label, key_prefix=plain[:8], key_hash=hash_key(plain), created_at=now,
        expires_at=now + timedelta(hours=settings.key_once_hours) if once else now + timedelta(days=ttl_days) if ttl_days else None,
        single_use=once,
    )
    session.add(row)
    session.commit()
    return row, plain


def key_state(row: PrintKey, now: datetime) -> str:
    """The one place that says what a key is: revoked | used | expired | disabled | active."""
    if row.revoked_at is not None:
        return "revoked"
    if row.used_at is not None:
        return "used"
    expires = aware(row.expires_at)
    if expires is not None and now >= expires:
        return "expired"
    return "disabled" if row.disabled_at is not None else "active"


def vanishes_at(row: PrintKey) -> datetime | None:
    """When a revoked or expired key leaves the list (None: it stays, or it is not over)."""
    if settings.key_history_days <= 0:
        return None
    end = aware(row.revoked_at) or aware(row.expires_at)
    return end + timedelta(days=settings.key_history_days) if end is not None else None


def listing(rows: list[PrintKey], now: datetime) -> list[dict]:
    """What the key pages show: single-use keys that have printed are gone; each row gets its state."""
    out = []
    for k in rows:
        state = key_state(k, now)
        if state == "used":
            continue
        out.append({"k": k, "state": state, "usable": state == "active",
                    "vanishes": vanishes_at(k) if state in ("revoked", "expired") else None})
    return out


def key_is_usable(row: PrintKey, now: datetime) -> bool:
    return key_state(row, now) == "active"


def in_grace(row: PrintKey, now: datetime) -> bool:
    """A single-use key whose print was accepted a moment ago: it still answers status queries (never a new job)."""
    used = aware(row.used_at)
    return (row.single_use and used is not None and row.revoked_at is None and row.disabled_at is None
            and now < used + timedelta(seconds=settings.key_once_grace_seconds))


def verify_key(session: Session, user: User, plain: str, ip: str | None = None, now: datetime | None = None,
               read_only: bool = False) -> PrintKey | None:
    """The key belongs to this user, is not revoked, disabled or expired, and (single use) has not printed yet.
    `read_only`: the caller only asks about jobs, so a single-use key that has just printed is still good."""
    if not plain.startswith(KEY_TAG) or len(plain) > 128:
        return None
    now = now or datetime.now(UTC)
    row = session.scalars(select(PrintKey).where(PrintKey.key_hash == hash_key(plain))).first()
    if row is None or row.user_id != user.id or not (key_is_usable(row, now) or (read_only and in_grace(row, now))):
        return None
    mono = time.monotonic()
    if mono - _last_touch.get(row.id, -TOUCH_EVERY) >= TOUCH_EVERY:
        _last_touch[row.id] = mono
        row.last_used_at, row.last_used_ip = now, (ip or "")[:64] or None
        session.commit()
    return row


def consume(session: Session, row: PrintKey, now: datetime | None = None) -> bool:
    """A single-use key has printed. True for the first caller only: two requests racing for it, one wins."""
    if not row.single_use:
        return False
    done = session.execute(update(PrintKey).where(PrintKey.id == row.id, PrintKey.used_at.is_(None))
                           .values(used_at=now or datetime.now(UTC)))
    session.commit()
    return done.rowcount == 1


def revoke_key(session: Session, row: PrintKey, now: datetime | None = None) -> bool:
    if row.revoked_at is not None:
        return False
    row.revoked_at = now or datetime.now(UTC)
    session.commit()
    return True


def revoke_all(session: Session, user: User, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    rows = [k for k in session.scalars(select(PrintKey).where(PrintKey.user_id == user.id, PrintKey.revoked_at.is_(None)))
            if key_state(k, now) in ("active", "disabled")]  # an expired key is already over
    for r in rows:
        r.revoked_at = now
    session.commit()
    return len(rows)


def ttl_days_for(user: User, radius_days: int, local_days: int) -> int:
    """The longest validity this account may pick."""
    return local_days if user.provider == "local" else radius_days


def allowed_durations(user: User) -> list[int]:
    """The durations (days) the menu offers: up to the account's maximum; when even the shortest is too long, the maximum alone."""
    cap = ttl_days_for(user, settings.key_ttl_days_radius, settings.key_ttl_days_local)
    return [d for d in DURATIONS if d <= cap] or [max(cap, 1)]


def duration_label(days: int) -> str:
    return {30: _("1 month"), 90: _("3 months"), 180: _("6 months"), 365: _("1 year"), 548: _("1 year and 6 months"),
            730: _("2 years")}.get(days) or _("%(n)s days", n=days)


def duration_choices(user: User) -> list[tuple[str, str]]:
    """(value, label) for the menu of a new key: the durations this account may pick, then the single-use one."""
    return [(str(d), duration_label(d)) for d in allowed_durations(user)] + [
        (ONCE, _("one print only (void after %(h)s hours if unused)", h=settings.key_once_hours))]


def default_duration(user: User) -> int:
    allowed = allowed_durations(user)
    return DEFAULT_DURATION if DEFAULT_DURATION in allowed else max(allowed)


def parse_duration(user: User, choice: str) -> tuple[int | None, bool]:
    """The value of the menu -> (days, once). Anything that is not on the user's menu raises ValueError."""
    if choice == ONCE:
        return None, True
    if choice.isdigit() and int(choice) in allowed_durations(user):
        return int(choice), False
    raise ValueError(_("choose one of the validity periods in the list"))


def disable_key(session: Session, row: PrintKey, now: datetime | None = None) -> bool:
    """An administrator switches a key off. expires_at is not touched: the clock keeps running."""
    if row.revoked_at is not None or row.disabled_at is not None:
        return False
    row.disabled_at = now or datetime.now(UTC)
    session.commit()
    return True


def enable_key(session: Session, row: PrintKey) -> bool:
    if row.revoked_at is not None or row.disabled_at is None:
        return False
    row.disabled_at = None
    session.commit()
    return True


def extend_key(session: Session, row: PrintKey, days: int, now: datetime | None = None) -> bool:
    """New expiry = now + days (single-use keys can not be extended; a revoked one stays revoked)."""
    if row.revoked_at is not None or row.single_use:
        return False
    row.expires_at = (now or datetime.now(UTC)) + timedelta(days=days)
    row.expiry_notified_at = None
    session.commit()
    return True


def purge(session: Session, now: datetime | None = None) -> int:
    """Delete what nobody needs to see any more: single-use keys past their grace, and revoked / expired keys
    SPOOLSTEWARD_KEY_HISTORY_DAYS after the day they ended (0 = keep them)."""
    now = now or datetime.now(UTC)
    gone = 0
    for k in session.scalars(select(PrintKey).where((PrintKey.used_at.is_not(None)) | (PrintKey.revoked_at.is_not(None))
                                                    | (PrintKey.expires_at.is_not(None)))).all():
        used, revoked, expires = aware(k.used_at), aware(k.revoked_at), aware(k.expires_at)
        history = timedelta(days=settings.key_history_days)
        drop = (used is not None and now >= used + timedelta(seconds=settings.key_once_grace_seconds)
                or settings.key_history_days > 0 and (revoked is not None and now >= revoked + history
                                                      or expires is not None and now >= expires + history))
        if drop:
            session.delete(k)
            gone += 1
    session.commit()
    return gone

