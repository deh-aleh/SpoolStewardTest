import hashlib
import hmac
import os
import time

from spoolsteward.auth.base import AuthProvider


class CachedAuth:
    """Caches successful logins only; keeps an HMAC of the password, never the password."""

    def __init__(self, inner: AuthProvider, ttl: int, clock=time.monotonic):
        self.inner, self.ttl, self.clock = inner, ttl, clock
        self.name = inner.name
        self._key = os.urandom(32)
        self._ok: dict[tuple[str, bytes], float] = {}

    def _digest(self, password: str) -> bytes:
        return hmac.new(self._key, password.encode(), hashlib.sha256).digest()

    async def authenticate(self, username: str, password: str) -> bool:
        k = (username, self._digest(password))
        now = self.clock()
        if self._ok.get(k, 0) > now:
            return True
        if await self.inner.authenticate(username, password):
            if len(self._ok) > 1000:  # drop expired entries so the dict cannot grow without bound
                self._ok = {key: exp for key, exp in self._ok.items() if exp > now}
            self._ok[k] = now + self.ttl
            return True
        self._ok.pop(k, None)
        return False
