import time

from app.auth.cache import CachedAuth
from app.auth.radius import RadiusProvider
from app.auth.throttle import FINGERPRINT_SEP, Throttle, register_failure
from tests.conftest import requires_radius


class Stub:
    name = "stub"

    def __init__(self, users):
        self.users, self.calls = users, 0

    async def authenticate(self, u, p):
        self.calls += 1
        return self.users.get(u) == p


async def test_cache_hits_and_expiry():
    now = [0.0]
    inner = Stub({"a": "pw"})
    c = CachedAuth(inner, ttl=60, clock=lambda: now[0])
    assert await c.authenticate("a", "pw") and await c.authenticate("a", "pw")
    assert inner.calls == 1
    now[0] = 61
    assert await c.authenticate("a", "pw")
    assert inner.calls == 2


async def test_cache_never_accepts_wrong_password_or_caches_failures():
    inner = Stub({"a": "pw"})
    c = CachedAuth(inner, ttl=60)
    assert await c.authenticate("a", "pw")
    assert not await c.authenticate("a", "other")
    assert not await c.authenticate("a", "other")
    assert inner.calls == 3  # success cached; each failure hits the backend


def test_throttle_blocks_and_recovers():
    now = [0.0]
    t = Throttle(limit=3, window=100, clock=lambda: now[0])
    for _ in range(3):
        t.fail("k")
    assert t.blocked("k")
    now[0] = 101
    assert not t.blocked("k")
    t.fail("k")
    t.clear("k")
    assert not t.blocked("k")


@requires_radius
async def test_radius_real_server():
    p = RadiusProvider("127.0.0.1", 1812, "testing123", timeout=2, retries=1)
    assert await p.authenticate("mario", "mario123")
    assert not await p.authenticate("mario", "wrong")
    assert not await p.authenticate("nobody", "x")
    assert not await p.authenticate("", "")


async def test_radius_unreachable_fails_closed():
    p = RadiusProvider("127.0.0.1", 9, "x", timeout=1, retries=1)  # nothing listens on udp/9
    assert not await p.authenticate("mario", "mario123")


async def test_cache_does_not_grow_without_bound():
    now = [0.0]
    inner = Stub({f"u{i}": "pw" for i in range(1500)})
    c = CachedAuth(inner, ttl=10, clock=lambda: now[0])
    for i in range(1500):
        now[0] += 1                       # entries expire after 10 s
        assert await c.authenticate(f"u{i}", "pw")
    assert len(c._ok) < 1100


@requires_radius
async def test_radius_reject_reason_is_logged_without_the_password(caplog):
    import logging
    p = RadiusProvider("127.0.0.1", 1812, "testing123", timeout=2, retries=1)
    with caplog.at_level(logging.WARNING, logger="app.auth.radius"):
        assert not await p.authenticate("mario", "S3cretWrong")
    assert "RADIUS rejected user='mario'" in caplog.text
    assert "S3cretWrong" not in caplog.text


def test_a_user_and_ip_lockout_made_by_failed_logins_survives_a_restart(tmp_path):
    f = tmp_path / "lock.json"
    t = Throttle(limit=2, window=100, path=f)
    for pw in ("one", "two"):                     # the keys the portal and the IPP gateway really use: "u:name|ip" contains "|"
        register_failure(t, "u:mario|10.0.0.5", "ip:10.0.0.5", pw)
    assert t.blocked("u:mario|10.0.0.5")
    assert Throttle(limit=2, window=100, path=f).blocked("u:mario|10.0.0.5")


def test_a_lockout_survives_a_restart_but_not_its_window(tmp_path):
    f = tmp_path / "lock.json"
    t = Throttle(limit=3, window=100, path=f)
    for _ in range(3):
        t.fail("mario")
    t.fail("x" + FINGERPRINT_SEP + "fingerprint")  # fingerprints of a wrong password are not written
    assert t.blocked("mario") and f.is_file() and oct(f.stat().st_mode & 0o777) == "0o600"
    assert "fingerprint" not in f.read_text()
    again = Throttle(limit=3, window=100, path=f)  # the gateway restarts
    assert again.blocked("mario") and not again.blocked("luigi")
    again.clear("mario")
    assert not Throttle(limit=3, window=100, path=f).blocked("mario")
    old = Throttle(limit=3, window=100, path=f)
    for _ in range(3):
        old.fail("luigi")
    time.sleep(0.05)
    assert not Throttle(limit=3, window=0.02, path=f).blocked("luigi")    # failures older than the window are forgotten


def test_a_broken_or_unwritable_lockout_file_never_stops_the_gateway(tmp_path):
    f = tmp_path / "lock.json"
    f.write_text("{not json")
    t = Throttle(limit=2, window=100, path=f)     # reads garbage: starts empty
    t.fail("a")
    t.fail("a")
    assert t.blocked("a")
    t2 = Throttle(limit=2, window=100, path=tmp_path / "no" / "such" / "dir" / "x.json")
    t2.fail("a")
    t2.fail("a")
    assert t2.blocked("a")                        # the file can not be written: memory only, no exception
