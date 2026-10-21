import hashlib
import hmac
import json
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path

log = logging.getLogger("spoolsteward.throttle")


_FINGERPRINT_KEY = os.urandom(32)  # per process: fingerprints are only ever compared within one run


def password_fingerprint(password: str) -> str:
    """Lets the throttle tell "the same wrong password again" from "a new guess" without keeping the password."""
    return hmac.new(_FINGERPRINT_KEY, password.encode(), hashlib.sha256).hexdigest()[:24]


FINGERPRINT_SEP = "\x1f"  # only the keys that carry a password fingerprint contain it (user names and addresses can hold "|")


def register_failure(throttle: "Throttle", ukey: str, ikey: str, password: str) -> None:
    """Count a failed login. A client that retries the SAME wrong password (libcups does 3-5 times per
    request) counts once, so a single typo cannot lock a user out; every new guess counts."""
    dkey = f"{ukey}{FINGERPRINT_SEP}{password_fingerprint(password)}"
    if throttle.blocked(dkey, 1):
        return
    throttle.fail(dkey)
    throttle.fail(ukey)
    throttle.fail(ikey)


# Failed logins tolerated per window. Keyed per (user, ip) so a stranger cannot lock someone else out;
# the per-ip ceiling is higher because one IP may legitimately be a NAT or a proxy.
USER_FAILS, IP_FAILS = 5, 30


class Throttle:
    """Sliding-window failure counter; blocks a key after `limit` failures in `window` seconds.
    With `path` the failures are also kept in a small file, so a restart does not wipe a lock-out (the file holds only
    user names and addresses with the times of their failures, never passwords)."""

    SAVE_EVERY = 2.0   # seconds: a flood of failed logins must not become a flood of disk writes

    def __init__(self, limit: int, window: float, clock=time.monotonic, path: str | Path | None = None):
        self.limit, self.window, self.clock = limit, window, clock
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self.path = Path(path) if path else None
        self._saved = 0.0
        self._dirty = False
        self._load()

    # --- keeping the failures over a restart (times are stored as wall-clock time: the monotonic clock restarts) ---
    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text())
            now_wall, now = time.time(), self.clock()
            for key, stamps in data.items():
                kept = sorted(now - max(now_wall - t, 0.0) for t in stamps if isinstance(t, (int, float)) and now_wall - t < self.window)
                if isinstance(key, str) and kept:
                    self._fails[key] = deque(kept)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError):
            log.warning("the login lock-out file %s can not be read: starting with an empty one", self.path)

    def save(self, force: bool = False) -> None:
        """Write the failures to the file (at most every SAVE_EVERY seconds unless `force`). Never raises."""
        if self.path is None or not (force or self._dirty):
            return
        now = time.monotonic()
        if not force and now - self._saved < self.SAVE_EVERY:
            return
        self._saved, self._dirty = now, False
        now_wall, mono = time.time(), self.clock()
        data = {}
        for key in list(self._fails):
            if FINGERPRINT_SEP in key:   # fingerprints of a wrong password: only meaningful inside one run
                continue
            stamps = [round(now_wall - (mono - t), 3) for t in self._prune(key)]
            if stamps:
                data[key] = stamps
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except OSError:
            log.warning("the login lock-out can not be saved to %s: it will be lost at the next restart", self.path)

    def _prune(self, key: str) -> deque[float]:
        q = self._fails[key]
        cutoff = self.clock() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        if not q:
            self._fails.pop(key, None)
            return deque()
        return q

    def blocked(self, key: str, limit: int | None = None) -> bool:
        return len(self._prune(key)) >= (limit or self.limit)

    def fail(self, key: str) -> None:
        self._prune(key)
        self._fails[key].append(self.clock())
        self._dirty = True
        self.save(force=len(self._fails[key]) >= self.limit)   # the failure that starts a lock-out is never left to the timer

    def clear(self, key: str) -> None:
        if self._fails.pop(key, None) is not None:
            self._dirty = True
            self.save()
