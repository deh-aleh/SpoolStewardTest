"""Pins what `spoolsteward.mdns.main` announces, updates and withdraws, in which order, with zeroconf, CUPS and the gateway faked."""

import pytest

from spoolsteward import mdns
from spoolsteward.config import settings


class Stop(Exception):
    pass


class FakeZC:
    instances: list = []

    def __init__(self, ip_version=None, interfaces=None):
        self.index = len(FakeZC.instances)
        FakeZC.instances.append(self)
        self.interfaces = interfaces
        self.ops = FakeZC.ops

    async def async_register_service(self, info, allow_name_change=True, cooperating_responders=False):
        self.ops.append((self.index, "register", info.type, info.name, allow_name_change, cooperating_responders))

    async def async_update_service(self, info):
        self.ops.append((self.index, "update", info.type, info.name))

    async def async_unregister_service(self, info):
        self.ops.append((self.index, "unregister", info.type, info.name))

    async def async_unregister_all_services(self):
        self.ops.append((self.index, "unregister_all"))

    async def async_close(self):
        self.ops.append((self.index, "close"))


def q(name, **extra):
    return {"name": name, "model": f"Model {name}", "info": f"Info {name}", **extra}


@pytest.fixture
def env(monkeypatch):
    e = type("E", (), {})()
    e.ops, e.calls, e.sleeps = [], [], []
    FakeZC.instances, FakeZC.ops = [], e.ops
    e.queues = [[q("a"), q("b")]]            # per iteration; the last one repeats
    e.policies = [None]
    e.scanner = {"model": "Scan 1", "uuid": "u1", "root": "eSCL", "adf": False, "color": True}
    e.stop_after = 1
    monkeypatch.setattr(mdns, "AsyncZeroconf", FakeZC)
    monkeypatch.setattr(mdns, "setup_logging", lambda *a: e.calls.append(("logging", a)))
    monkeypatch.setattr(mdns, "make_upstream", lambda: "UP")
    for k, v in dict(mdns_hostname="gw.example.org", mdns_port=0, port=8443, mdns_addresses="10.0.0.5, 10.0.0.6",
                     portal_url="", mdns_interval=7.0).items():
        monkeypatch.setattr(settings, k, v)

    def pick(seq, i):
        return seq[min(i, len(seq) - 1)]

    async def list_queues(app, probe=True):
        e.calls.append(("list_queues", app.state.upstream, app.state.queues_cache, probe))
        return pick(e.queues, len(e.sleeps))

    async def fetch_policy(port, announce=None):
        e.calls.append(("fetch_policy", port, announce))
        return pick(e.policies, len(e.sleeps))

    async def scanner_info(port, root="eSCL"):
        e.calls.append(("scanner_info", port, root))
        return None if e.scanner is None else {**e.scanner, "root": root}

    async def sleep(n):
        e.sleeps.append(n)
        e.ops.append(("sleep",))
        if len(e.sleeps) >= e.stop_after:
            raise Stop

    monkeypatch.setattr(mdns, "list_queues", list_queues)
    monkeypatch.setattr(mdns, "fetch_policy", fetch_policy)
    monkeypatch.setattr(mdns, "scanner_info", scanner_info)
    monkeypatch.setattr(mdns.asyncio, "sleep", sleep)
    return e


async def run(e):
    with pytest.raises(Stop):
        await mdns.main()


async def test_first_pass_announces_printers_and_the_default_scanner(env):
    await run(env)
    P, U, S = mdns.SERVICE, mdns.UNIVERSAL, mdns.SCAN_SERVICE
    assert env.ops == [
        (0, "register", P, f"Info a @ gw.{P}", False, True),
        (1, "register", U, f"Info a @ gw.{P}", False, True),
        (0, "register", P, f"Info b @ gw.{P}", False, True),
        (1, "register", U, f"Info b @ gw.{P}", False, True),
        (0, "register", S, f"Scan 1 (scanner) @ gw.{S}", False, True),
        ("sleep",),
        (0, "unregister_all"), (0, "close"), (1, "unregister_all"), (1, "close"),
    ]
    assert [z.interfaces for z in FakeZC.instances] == [["10.0.0.5", "10.0.0.6"]] * 2
    assert env.sleeps == [7.0]
    assert env.calls[0][0] == "logging"
    assert env.calls[1:] == [
        ("list_queues", "UP", None, False),
        ("fetch_policy", 8443, {"names": ["gw.example.org", "gw.local"], "addresses": ["10.0.0.5", "10.0.0.6"]}),
        ("scanner_info", 8443, "eSCL"),
    ]


@pytest.mark.parametrize("portal_url, host, expected", [
    ("", "gw.example.org", "https://gw.example.org:8443/me"),
    ("https://print.example.org/", "", "https://print.example.org/me"),
    ("https://print.example.org/me", "", "https://print.example.org/me"),
    ("https://print.example.org:9000", "", "https://print.example.org:9000/me"),
])
async def test_portal_url_and_host_defaults(env, monkeypatch, portal_url, host, expected):
    monkeypatch.setattr(settings, "portal_url", portal_url)
    monkeypatch.setattr(settings, "mdns_hostname", host)
    monkeypatch.setattr(settings, "mdns_port", 0)
    seen = []
    real = mdns.build_services

    def spy(queues, h, addresses, port, portal):
        seen.append((h, addresses, port, portal))
        return real(queues, h, addresses, port, portal)
    monkeypatch.setattr(mdns, "build_services", spy)
    await run(env)
    if host:
        assert seen == [(host, ["10.0.0.5", "10.0.0.6"], 8443, expected)]
    else:
        assert seen == [("print.example.org", ["10.0.0.5", "10.0.0.6"], 8443, expected)]


async def test_mdns_port_and_default_address(env, monkeypatch):
    monkeypatch.setattr(settings, "mdns_port", 9999)
    monkeypatch.setattr(settings, "mdns_addresses", " , ")
    monkeypatch.setattr(mdns, "default_address", lambda: ["192.0.2.9"])
    await run(env)
    assert [z.interfaces for z in FakeZC.instances] == [["192.0.2.9"]] * 2
    assert ("fetch_policy", 9999, {"names": ["gw.example.org", "gw.local"], "addresses": ["192.0.2.9"]}) in env.calls


async def test_changes_between_passes(env):
    env.stop_after = 4
    env.queues = [[q("a"), q("b")], [q("b", location="Room 2"), q("c")], [q("b", location="Room 2"), q("c")], []]
    env.policies = [None,
                    {"maintenance": False, "hidden": set(), "offline": set(), "scanners": [{"root": "eSCL2", "name": "Office"}]},
                    None,                                  # gateway unreachable: the last policy stays
                    {"maintenance": True, "hidden": set(), "offline": set(), "scanners": [{"root": "x"}]}]
    await run(env)
    P, U, S = mdns.SERVICE, mdns.UNIVERSAL, mdns.SCAN_SERVICE
    reg = lambda i, t, n: (i, "register", t, n, False, True)   # noqa: E731
    withdrawn_a = [(0, "unregister", P, f"Info a @ gw.{P}"), (1, "unregister", U, f"Info a @ gw.{P}")]
    # pass 1: a, b, default scanner
    assert env.ops[:6] == [reg(0, P, f"Info a @ gw.{P}"), reg(1, U, f"Info a @ gw.{P}"), reg(0, P, f"Info b @ gw.{P}"),
                           reg(1, U, f"Info b @ gw.{P}"), reg(0, S, f"Scan 1 (scanner) @ gw.{S}"), ("sleep",)]
    # pass 2: a and the default scanner leave, b changes (location), c and "Office" arrive
    rest = env.ops[6:]
    pass2 = rest[:rest.index(("sleep",))]
    assert {(o[1], o[3]) for o in pass2 if o[1] == "unregister"} == {("unregister", f"Info a @ gw.{P}"), ("unregister", f"Scan 1 (scanner) @ gw.{S}")}
    assert (0, "update", P, f"Info b @ gw.{P}") in pass2 and (1, "update", U, f"Info b @ gw.{P}") in pass2
    assert reg(0, P, f"Info c @ gw.{P}") in pass2 and reg(0, S, f"Office (scanner) @ gw.{S}") in pass2
    assert withdrawn_a[0] in pass2
    # pass 3: nothing changed (the policy is the one of pass 2)
    rest = rest[len(pass2) + 1:]
    pass3 = rest[:rest.index(("sleep",))]
    assert pass3 == []
    # pass 4: maintenance: everything is withdrawn, no scanner is looked up
    rest = rest[len(pass3) + 1:]
    pass4 = rest[:rest.index(("sleep",))]
    assert {o[3] for o in pass4} == {f"Info b @ gw.{P}", f"Info c @ gw.{P}", f"Office (scanner) @ gw.{S}"}
    assert all(o[1] == "unregister" for o in pass4)
    assert [c for c in env.calls if c[0] == "scanner_info"] == [("scanner_info", 8443, "eSCL"), ("scanner_info", 8443, "eSCL2"), ("scanner_info", 8443, "eSCL2")]
    assert env.ops[-4:] == [(0, "unregister_all"), (0, "close"), (1, "unregister_all"), (1, "close")]


async def test_a_scanner_that_does_not_answer_is_not_announced(env):
    env.scanner = None
    await run(env)
    assert [o[3] for o in env.ops if o[1:2] == ("register",)] == [f"Info a @ gw.{mdns.SERVICE}", f"Info a @ gw.{mdns.SERVICE}",
                                                                     f"Info b @ gw.{mdns.SERVICE}", f"Info b @ gw.{mdns.SERVICE}"]


async def test_scanner_without_a_label_uses_its_model_and_hidden_queues_are_dropped(env):
    env.policies = [{"maintenance": False, "hidden": {"a"}, "offline": {"b"}, "scanners": [{"root": "r1"}, {"root": "r2", "name": "Named"}]}]
    env.queues = [[q("a"), q("b"), q("c")]]
    await run(env)
    names = [o[3] for o in env.ops if o[1:2] == ("register",)]
    P, S = mdns.SERVICE, mdns.SCAN_SERVICE
    assert names == [f"Info c @ gw.{P}", f"Info c @ gw.{P}", f"Scan 1 (scanner) @ gw.{S}", f"Named (scanner) @ gw.{S}"]


async def test_a_pass_that_fails_does_not_end_the_announcer(env, caplog):
    env.stop_after = 2
    env.queues = [[q("a")]]
    calls = []
    real = mdns.build_services

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("zeroconf said no")
        return real(*a, **k)
    mdns.build_services = flaky
    try:
        await run(env)
    finally:
        mdns.build_services = real
    assert "announcement pass failed" in caplog.text
    assert [o[3] for o in env.ops if o[1:2] == ("register",) and o[3].startswith("Info a")] == [f"Info a @ gw.{mdns.SERVICE}"] * 2   # announced by the second pass


@pytest.mark.parametrize("why, words", [("no_key", "SPOOLSTEWARD_SECRET_KEY=<value>"), ("token", "--force-recreate gateway mdns")])
async def test_when_the_gateway_refuses_nothing_is_announced_and_the_log_says_what_to_do(env, caplog, why, words):
    env.policies = [{mdns.REFUSED: why}]
    env.stop_after = 3
    await run(env)
    assert not [o for o in env.ops if o[1:2] == ("register",)]
    assert [m for m in caplog.messages if "mDNS is OFF" in m and words in m] and len([m for m in caplog.messages if "mDNS is OFF" in m]) == 1   # said once, not at every pass


async def test_something_that_was_announced_is_withdrawn_when_the_gateway_starts_refusing(env):
    env.stop_after = 2
    env.policies = [None, {mdns.REFUSED: "token"}]
    await run(env)
    withdrawn = [o for o in env.ops if o[1:2] == ("unregister",)]
    assert {o[3] for o in withdrawn} == {f"Info a @ gw.{mdns.SERVICE}", f"Info b @ gw.{mdns.SERVICE}", f"Scan 1 (scanner) @ gw.{mdns.SCAN_SERVICE}"}


async def test_fetch_policy_refuses_without_a_key_and_on_404(monkeypatch):
    import httpx

    monkeypatch.setattr(settings, "secret_key", "")
    assert await mdns.fetch_policy(8631) == {mdns.REFUSED: "no_key"}      # no network call at all
    monkeypatch.setattr(settings, "secret_key", "k" * 64)

    class Client:
        status = 404

        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return httpx.Response(self.status, json={"maintenance": False, "hidden": [], "offline": [], "scanners": []})

    monkeypatch.setattr(mdns.httpx, "AsyncClient", Client)
    assert await mdns.fetch_policy(8631) == {mdns.REFUSED: "token"}
    Client.status = 500
    assert await mdns.fetch_policy(8631) is None                          # an unwell gateway is not a refusal: keep what was known
    Client.status = 200
    assert (await mdns.fetch_policy(8631))["maintenance"] is False
