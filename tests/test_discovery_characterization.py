"""Pins the order of calls, the notes and the state `discovery._create_new` leaves behind, with every collaborator faked."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.config import settings
from app.policy import discovery


def uri(n):
    return f"ipp://ipp-usb:{n}/ipp/print"


def bridge(port, mfg, mdl):
    return {"port": port, "uri": uri(port), "model": f"{mfg} {mdl}".strip(), "id": {"mfg": mfg, "mdl": mdl}}


def dev(n, mfg, mdl):
    return {"uri": f"usb://{mfg}/{mdl}?serial={n}", "model": f"{mfg} {mdl}".strip(), "id": {"mfg": mfg, "mdl": mdl}}


@pytest.fixture
def world(monkeypatch):
    w = SimpleNamespace(log=[], queues=[], bridge=[], devices=[], ppds={}, refuse=set())
    w.app = SimpleNamespace(state=SimpleNamespace(upstream="UP", sessionmaker="SM"))

    async def all_queues(up):
        w.log.append(("all_queues", up))
        return w.queues

    async def bridge_printers():
        w.log.append(("bridge",))
        return w.bridge

    async def usb_devices(up):
        w.log.append(("devices", up))
        return w.devices

    async def find_ppd(up, ident, model):
        w.log.append(("find_ppd", dict(ident), model))
        return w.ppds.get(model)

    async def add_queue(app, name, device_uri, info, ppd):
        w.log.append(("add", name, device_uri, info, ppd))
        return device_uri not in w.refuse

    def created(sm, name, **detail):
        w.log.append(("created", sm, name, detail))

    monkeypatch.setattr(discovery, "all_queues", all_queues)
    monkeypatch.setattr(discovery, "bridge_printers", bridge_printers)
    monkeypatch.setattr(discovery, "usb_devices", usb_devices)
    monkeypatch.setattr(discovery, "find_ppd", find_ppd)
    monkeypatch.setattr(discovery, "add_queue", add_queue)
    monkeypatch.setattr(discovery, "_created", created)
    monkeypatch.setattr(settings, "usb_direct", True)
    return w


async def test_cups_not_answering_creates_nothing(world):
    world.queues = None
    world.bridge = [bridge(1, "HP", "A")]
    notes = []
    assert await discovery._create_new(world.app, notes) == 0
    assert notes == [] and world.log == [("all_queues", "UP")]
    assert not hasattr(world.app.state, "usb_present")


async def test_bridge_pass_then_stop_when_usb_direct_is_off(world, monkeypatch):
    monkeypatch.setattr(settings, "usb_direct", False)
    world.queues = [{"name": "old", "device_uri": uri(1)}]
    world.bridge = [bridge(1, "HP", "Have"), bridge(2, "Epson", "L1"), bridge(3, "Epson", "L1"), bridge(4, "Nope", "N"), bridge(5, "", "")]
    world.refuse = {uri(4)}
    world.app.state.discovery_refused = {discovery._norm_uri(uri(5)): datetime.now(UTC)}
    notes = []
    assert await discovery._create_new(world.app, notes) == 2
    assert world.log == [
        ("all_queues", "UP"), ("bridge",),
        ("add", "Epson_L1", uri(2), "Epson L1", "everywhere"),
        ("created", "SM", "Epson_L1", {"model": "Epson L1", "via": "ipp-usb"}),
        ("add", "Epson_L1_2", uri(3), "Epson L1", "everywhere"),
        ("created", "SM", "Epson_L1_2", {"model": "Epson L1", "via": "ipp-usb"}),
        ("add", "Nope_N", uri(4), "Nope N", "everywhere"),
    ]
    assert notes == [{"kind": "refused", "model": "Nope N"}, {"kind": "refused", "model": uri(5)}]
    refused = world.app.state.discovery_refused
    assert discovery._norm_uri(uri(4)) in refused and discovery._norm_uri(uri(2)) not in refused
    assert not hasattr(world.app.state, "usb_present") and not hasattr(world.app.state, "usb_unserved")


async def test_usb_pass_all_branches(world):
    world.queues = [{"name": "have", "device_uri": dev(1, "A", "Have")["uri"]}]
    world.bridge = [bridge(10, "HP", "Smart Tank 7300")]
    d_have = dev(1, "A", "Have")
    d_served = dev(2, "HP", "Smart Tank 7300")                  # same (mfg, mdl) as a bridge printer
    d_sub = dev(3, "Other", "Tank")                              # mdl inside a bridge model name
    d_first = dev(4, "B", "First")                               # first sighting
    d_nodrv = dev(5, "C", "NoDrv")
    d_cool = dev(6, "D", "Cool")
    d_ok = dev(7, "E", "Good")
    d_bad = dev(8, "F", "Bad")
    d_nomdl = dev(9, "G", "")
    world.devices = [d_have, d_served, d_sub, d_first, d_nodrv, d_cool, d_ok, d_bad, d_nomdl]
    world.ppds = {d_cool["model"]: "cool.ppd", d_ok["model"]: "good.ppd", d_bad["model"]: "bad.ppd", d_nomdl["model"]: "nomdl.ppd"}
    world.refuse = {d_bad["uri"], uri(10)}
    world.app.state.usb_unserved = {d_nodrv["uri"]: 1, d_cool["uri"]: 5, d_ok["uri"]: 1, d_bad["uri"]: 2, d_nomdl["uri"]: 1, "usb://gone": 9}
    world.app.state.discovery_refused = {discovery._norm_uri(d_cool["uri"]): datetime.now(UTC)}
    notes = []
    assert await discovery._create_new(world.app, notes) == 2
    assert world.log == [
        ("all_queues", "UP"), ("bridge",),
        ("add", "HP_Smart_Tank_7300", uri(10), "HP Smart Tank 7300", "everywhere"),
        ("devices", "UP"),
        ("find_ppd", {"mfg": "C", "mdl": "NoDrv"}, "C NoDrv"),
        ("find_ppd", {"mfg": "D", "mdl": "Cool"}, "D Cool"),
        ("find_ppd", {"mfg": "E", "mdl": "Good"}, "E Good"),
        ("add", "E_Good", d_ok["uri"], "E Good", "good.ppd"),
        ("created", "SM", "E_Good", {"model": "E Good", "via": "usb", "driver": "good.ppd"}),
        ("find_ppd", {"mfg": "F", "mdl": "Bad"}, "F Bad"),
        ("add", "F_Bad", d_bad["uri"], "F Bad", "bad.ppd"),
        ("find_ppd", {"mfg": "G", "mdl": ""}, "G"),
        ("add", "G", d_nomdl["uri"], "G", "nomdl.ppd"),
        ("created", "SM", "G", {"model": "G", "via": "usb", "driver": "nomdl.ppd"}),
    ]
    assert notes == [{"kind": "refused", "model": "HP Smart Tank 7300"}, {"kind": "no_driver", "model": "C NoDrv"},
                     {"kind": "refused", "model": "D Cool"}, {"kind": "refused", "model": "F Bad"}]
    assert world.app.state.usb_present == {d["uri"] for d in world.devices}
    assert world.app.state.usb_unserved == {d_first["uri"]: 1, d_nodrv["uri"]: 2, d_cool["uri"]: 6, d_ok["uri"]: 2,
                                            d_bad["uri"]: 3, d_nomdl["uri"]: 2}


async def test_usb_devices_unknown_keeps_nothing(world):
    world.queues = []
    world.devices = None
    world.app.state.usb_unserved = {"usb://x": 1}
    notes = []
    assert await discovery._create_new(world.app, notes) == 0
    assert world.app.state.usb_present is None and world.app.state.usb_unserved == {} and notes == []
    assert world.log == [("all_queues", "UP"), ("bridge",), ("devices", "UP")]
