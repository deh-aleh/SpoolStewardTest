"""Scan window: "my scan app is about to scan, accept it from this address".

Scan apps that cannot send a password (NAPS2 and most others) are refused while SPOOLSTEWARD_ESCL_ANONYMOUS is off. The person
presses a button on /me/scan: the gateway notes the address it sees and, for SPOOLSTEWARD_ESCL_WINDOW_SECONDS, accepts the
credential-less eSCL requests that come from that address, as that person (their quotas and permissions apply, the audit
names them). Everything else is still refused.

Nothing runs in the background: a window is a record with an end time that is compared when a request arrives. A scan
that started inside the window is tied to the address, so it is delivered even if the window ends meanwhile (see
spoolsteward/escl.py, "owner"); the window only decides whether a NEW scan may start.

Windows live in memory: a restart closes them, which is acceptable for a few minutes. What the server sees is the
connection's address (behind a proxy: whatever the proxy is configured to pass on), so NAT, IPv4/IPv6 and proxies are
limits of the method, written in the user guide.
"""

import ipaddress
import threading
import time
from dataclasses import dataclass

MAX_WINDOWS = 200          # open at the same time (every person has at most one): a cap against memory growth
MISMATCH_EVERY = 60.0      # the same (window, other address) is reported at most this often


def normalize_ip(raw: str | None) -> str:
    """The address in one canonical spelling ("" if it is not an address): IPv4-mapped IPv6 (::ffff:1.2.3.4) is the IPv4
    address, a zone ("fe80::1%eth0") is dropped."""
    if not raw:
        return ""
    try:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return ""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


@dataclass
class Window:
    ip: str
    user_id: int
    username: str
    opened: float
    expires: float


class Windows:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._by_user: dict[int, Window] = {}
        self._reported: dict[tuple[str, str], float] = {}

    def _purge(self, now: float) -> None:
        for uid in [u for u, w in self._by_user.items() if w.expires <= now]:
            del self._by_user[uid]
        for k in [k for k, t in self._reported.items() if now - t >= MISMATCH_EVERY]:
            del self._reported[k]

    def open(self, ip: str, user_id: int, username: str, seconds: int) -> Window | None:
        """Open (or renew) the person's window for this address. One window per person; if somebody else had one for the
        same address it ends (the last one pressed is the one that is expected). None = too many windows open."""
        now = self._clock()
        with self._lock:
            self._purge(now)
            for uid in [u for u, w in self._by_user.items() if w.ip == ip and u != user_id]:
                del self._by_user[uid]
            if user_id not in self._by_user and len(self._by_user) >= MAX_WINDOWS:
                return None
            w = self._by_user[user_id] = Window(ip, user_id, username, now, now + seconds)
            return w

    def close(self, user_id: int) -> bool:
        with self._lock:
            return self._by_user.pop(user_id, None) is not None

    def of_user(self, user_id: int) -> Window | None:
        now = self._clock()
        with self._lock:
            w = self._by_user.get(user_id)
            return w if w is not None and w.expires > now else None

    def for_ip(self, ip: str) -> Window | None:
        """The window that accepts this address right now."""
        now = self._clock()
        with self._lock:
            for w in self._by_user.values():
                if w.ip == ip and w.expires > now:
                    return w
        return None

    def others(self, ip: str) -> list[Window]:
        """Windows open for some other address: whoever pressed the button expected a scan from there."""
        now = self._clock()
        with self._lock:
            return [w for w in self._by_user.values() if w.ip != ip and w.expires > now]

    def mismatch_due(self, window_ip: str, got_ip: str) -> bool:
        """True (and remembered) when this pair has not been reported for a while: a host that keeps trying must not fill
        the audit and the person's bell."""
        now = self._clock()
        with self._lock:
            self._purge(now)
            key = (window_ip, got_ip)
            if key in self._reported:
                return False
            if len(self._reported) >= 1000:
                return False
            self._reported[key] = now
            return True

    def remaining(self, user_id: int) -> int:
        w = self.of_user(user_id)
        return max(0, int(w.expires - self._clock())) if w else 0
