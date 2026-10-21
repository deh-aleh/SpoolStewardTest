"""USB printers that find themselves (ipp-usb first, CUPS' own usb backend for the rest) and USB queues that leave by
themselves after a day's... after `offline_remove_hours` without an answer. A fake CUPS answers over IPP."""

import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from spoolsteward.config import settings
from spoolsteward.db.models import AdminAudit, PrinterOffline
from spoolsteward.ipp import codec
from spoolsteward.policy import discovery
from tests.conftest import csrf_of_page, make_user, portal_login

GOOD = "correct horse battery"
BRIDGE = "ipp://ipp-usb:60001/ipp/print"
BROTHER = "usb://Brother/DCP-7055?serial=E1"


class FakeCups:
    """Get-Printers / Get-Devices / Get-PPDs / Add-Modify-Printer / Delete-Printer over IPP, with `queues` [(name, uri)]."""

    def __init__(self, queues, devices=(), ppds=()):
        self.queues, self.devices, self.ppds = list(queues), list(devices), list(ppds)
        self.added, self.deleted, self.stopped, self.jobs, self.refuse_add = [], [], set(), {}, False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        msg = codec.decode(request.content)
        ok = codec.IppMessage((2, 0), 0, 1, [codec.Group(codec.TAG_OPERATION, [
            codec.Attr("attributes-charset", [codec.Val(codec.TAG_CHARSET, "utf-8")]),
            codec.Attr("attributes-natural-language", [codec.Val(codec.TAG_NATURAL_LANG, "en")])])])
        if msg.code == 0x4002:
            for name, uri in self.queues:
                ok.groups.append(codec.Group(codec.TAG_PRINTER, [
                    codec.Attr("printer-name", [codec.Val(codec.TAG_NAME, name)]),
                    codec.Attr("device-uri", [codec.Val(codec.TAG_URI, uri)]),
                    codec.Attr("printer-info", [codec.Val(codec.TAG_TEXT, f"Info of {name}")]),
                    codec.Attr("queued-job-count", [codec.Val(codec.TAG_INTEGER, self.jobs.get(name, 0))]),
                    codec.Attr("printer-is-accepting-jobs", [codec.Val(codec.TAG_BOOLEAN, name not in self.stopped)]),
                    codec.Attr("printer-state", [codec.Val(codec.TAG_ENUM, 3)])]))
        elif msg.code == 0x400B:
            # like CUPS: include-schemes is honoured only as a NAME (a keyword is silently ignored and every backend is listed)
            msg.get(codec.TAG_OPERATION, "include-schemes")
            attr = msg.group(codec.TAG_OPERATION).get("include-schemes")
            honoured = attr is not None and attr.values[0].tag == codec.TAG_NAME
            for uri, mfg, mdl in (self.devices + ([] if honoured else [("beh", "", ""), ("cups-pdf:/", "Generic", "CUPS-PDF Printer"), ("ipp", "", "")])):
                ok.groups.append(codec.Group(codec.TAG_PRINTER, [
                    codec.Attr("device-uri", [codec.Val(codec.TAG_URI, uri)]),
                    codec.Attr("device-id", [codec.Val(codec.TAG_TEXT, f"MFG:{mfg};MDL:{mdl};CLS:PRINTER;")]),
                    codec.Attr("device-make-and-model", [codec.Val(codec.TAG_TEXT, f"{mfg} {mdl}")])]))
        elif msg.code == 0x400C:
            for name, mfg, mdl in self.ppds:
                ok.groups.append(codec.Group(codec.TAG_PRINTER, [
                    codec.Attr("ppd-name", [codec.Val(codec.TAG_NAME, name)]),
                    codec.Attr("ppd-device-id", [codec.Val(codec.TAG_TEXT, f"MFG:{mfg};MDL:{mdl};")]),
                    codec.Attr("ppd-make-and-model", [codec.Val(codec.TAG_TEXT, f"{mfg} {mdl}, driver")])]))
        elif msg.code == 0x4003:
            if self.refuse_add:
                ok.code = 0x0500
                return httpx.Response(200, content=codec.encode(ok), headers={"content-type": "application/ipp"})
            name = msg.get(codec.TAG_OPERATION, "printer-uri").rsplit("/", 1)[-1]
            uri = msg.group(codec.TAG_PRINTER).get("device-uri").first
            self.added.append((name, uri, msg.get(codec.TAG_OPERATION, "ppd-name")))
            self.queues.append((name, uri))
        elif msg.code == 0x4004:
            name = msg.get(codec.TAG_OPERATION, "printer-uri").rsplit("/", 1)[-1]
            self.deleted.append(name)
            self.queues = [q for q in self.queues if q[0] != name]
        return httpx.Response(200, content=codec.encode(ok), headers={"content-type": "application/ipp"})


@pytest.fixture
async def usb(portal, monkeypatch):
    import spoolsteward.admin.printers as printers_mod
    import spoolsteward.cups_client as cc
    state = {"down": set(), "bridge": []}

    async def online(uri, usb_present=None):
        return uri not in state["down"]
    monkeypatch.setattr(cc, "is_online", online)
    monkeypatch.setattr(printers_mod, "is_online", online)

    async def bridge():
        return state["bridge"]
    monkeypatch.setattr(discovery, "bridge_printers", bridge)

    async def no_device(base):
        return {}
    monkeypatch.setattr(printers_mod, "_device", no_device)
    fake = FakeCups([("hp", BRIDGE), ("lan", "ipp://192.0.2.20:631/ipp/print")],
                    ppds=[("drv:///brlaser.drv/br7055w.ppd", "Brother", "DCP-7055W"), ("drv:///brlaser.drv/br7055.ppd", "Brother", "DCP-7055")])
    portal.fake, portal.state = fake, state
    portal.app.state.upstream = httpx.AsyncClient(transport=httpx.MockTransport(fake), base_url="http://localhost")
    portal.app.state.queues_cache = portal.app.state.printers_cache = None
    portal.app.state.usb_present = None
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    make_user(portal.sm, "boss2", password=GOOD, admin=True)
    await portal_login(portal, "boss", GOOD)
    yield portal
    await portal.app.state.upstream.aclose()


def bridge_printer(port, mfg, mdl):
    return {"port": port, "uri": f"ipp://ipp-usb:{port}/ipp/print", "model": f"{mfg} {mdl}", "id": {"mfg": mfg, "mdl": mdl}}


def audits(usb):
    with usb.sm() as s:
        return [(a.action, a.actor_name) for a in s.scalars(select(AdminAudit).order_by(AdminAudit.id)) if a.action.startswith("printer.auto")]


# ---------------------------------------------------------------- pieces

def test_device_ids_and_queue_names():
    assert discovery.parse_device_id("MFG:Brother;CMD:PJL,HBP;MDL:DCP-7055;CLS:PRINTER;") == {"mfg": "Brother", "cmd": "PJL,HBP", "mdl": "DCP-7055", "cls": "PRINTER"}
    assert discovery.parse_device_id("MANUFACTURER:HP;MODEL:Smart Tank;")["mdl"] == "Smart Tank"
    assert discovery.parse_device_id("") == {}
    assert discovery.queue_name("Brother DCP-7055", set()) == "Brother_DCP-7055"
    assert discovery.queue_name("Brother DCP-7055", {"brother_dcp-7055"}) == "Brother_DCP-7055_2"
    assert discovery.queue_name("§§", set()) == "printer"


def test_which_queues_hang_on_a_usb_cable():
    assert discovery.is_usb_queue("ipp://ipp-usb:60000/ipp/print") and discovery.is_usb_queue(BROTHER)
    assert not discovery.is_usb_queue("ipp://192.0.2.20:631/ipp/print") and not discovery.is_usb_queue("cups-pdf:/")


def test_bridge_port_range_is_parsed_and_capped(monkeypatch):
    assert discovery.bridge_ports()[0] == 60000 and len(discovery.bridge_ports()) == 100
    monkeypatch.setattr(settings, "usb_bridge_ports", "garbage")
    assert len(discovery.bridge_ports()) == 100
    monkeypatch.setattr(settings, "usb_bridge_ports", "100-60000")
    assert len(discovery.bridge_ports()) == 200


# ---------------------------------------------------------------- finding printers

async def test_a_new_ipp_usb_printer_gets_a_queue_once(usb):
    usb.state["bridge"] = [bridge_printer(60001, "HP", "Smart Tank 7300"), bridge_printer(60005, "Epson", "L3150")]   # 60001 already has its queue
    out = await discovery.run_once(usb.app)
    assert out["created"] == 1 and usb.fake.added == [("Epson_L3150", "ipp://ipp-usb:60005/ipp/print", "everywhere")]
    assert audits(usb) == [("printer.auto_add", "system")]
    assert (await discovery.run_once(usb.app))["created"] == 0 and len(usb.fake.added) == 1      # not added again


async def test_a_printer_without_ipp_over_usb_is_added_with_its_driver(usb):
    usb.fake.devices = [(BROTHER, "Brother", "DCP-7055")]
    assert (await discovery.run_once(usb.app))["created"] == 0          # first sighting: ipp-usb might just be restarting
    out = await discovery.run_once(usb.app)
    assert out["notes"] == [] and out["created"] == 1 and usb.fake.added == [("Brother_DCP-7055", BROTHER, "drv:///brlaser.drv/br7055.ppd")]  # not the "W" model
    assert usb.app.state.usb_present == {BROTHER}


async def test_ipp_usb_has_priority_over_the_usb_backend(usb):
    usb.state["bridge"] = [bridge_printer(60002, "Brother", "DCP-7055")]
    usb.fake.devices = [(BROTHER, "Brother", "DCP-7055")]          # the same device, seen by CUPS' usb backend too
    await discovery.run_once(usb.app)
    await discovery.run_once(usb.app)
    assert [a[1] for a in usb.fake.added] == ["ipp://ipp-usb:60002/ipp/print"]


async def test_a_usb_printer_without_a_driver_is_reported_not_added(usb):
    usb.fake.devices = [("usb://Obscure/X1?serial=1", "Obscure", "X1")]
    await discovery.run_once(usb.app)
    out = await discovery.run_once(usb.app)
    assert out["created"] == 0 and usb.fake.added == [] and out["notes"] == [{"kind": "no_driver", "model": "Obscure X1"}]
    page = (await usb.get("/admin/printers")).text
    assert "Obscure X1" in page and "driver" in page


async def test_discovery_can_be_switched_off(usb, monkeypatch):
    monkeypatch.setattr(settings, "auto_discover", False)
    usb.state["bridge"] = [bridge_printer(60005, "Epson", "L3150")]
    assert (await discovery.run_once(usb.app))["created"] == 0 and usb.fake.added == []


async def test_look_now_button(usb):
    usb.state["bridge"] = [bridge_printer(60005, "Epson", "L3150")]
    tok = await csrf_of_page(usb, "/admin/printers")
    r = await usb.post("/admin/printers/discover", data={"csrf_token": tok})
    assert r.status_code == 303 and usb.fake.added
    assert "1 new printer added" in (await usb.get("/admin/printers")).text or "Epson_L3150" in (await usb.get("/admin/printers")).text


# ---------------------------------------------------------------- queues that leave

async def test_an_unreachable_usb_queue_is_removed_after_the_wait_but_a_network_one_never(usb):
    usb.state["down"] = {BRIDGE, "ipp://192.0.2.20:631/ipp/print"}
    await discovery.run_once(usb.app)
    with usb.sm() as s:
        rows = {r.printer: r.since for r in s.scalars(select(PrinterOffline))}
    assert set(rows) == {"hp"}                                    # only the USB queue is tracked
    await discovery.run_once(usb.app)
    assert usb.fake.deleted == []                                 # not yet
    with usb.sm() as s:
        s.get(PrinterOffline, "hp").since = datetime.now(UTC) - timedelta(hours=8, minutes=1)
        s.commit()
    out = await discovery.run_once(usb.app)
    assert out["removed"] == ["hp"] and usb.fake.deleted == ["hp"] and [q[0] for q in usb.fake.queues] == ["lan"]
    with usb.sm() as s:
        assert s.scalars(select(PrinterOffline)).first() is None
    assert audits(usb) == [("printer.auto_remove", "system")]


async def test_coming_back_cancels_the_countdown(usb):
    usb.state["down"] = {BRIDGE}
    await discovery.run_once(usb.app)
    usb.state["down"] = set()
    await discovery.run_once(usb.app)
    with usb.sm() as s:
        assert s.scalars(select(PrinterOffline)).first() is None
    assert usb.fake.deleted == []


async def test_the_wait_can_be_switched_off(usb, monkeypatch):
    monkeypatch.setattr(settings, "offline_remove_hours", 0)
    usb.state["down"] = {BRIDGE}
    await discovery.run_once(usb.app)
    with usb.sm() as s:
        s.get(PrinterOffline, "hp").since = datetime.now(UTC) - timedelta(days=30)
        s.commit()
    assert (await discovery.run_once(usb.app))["removed"] == [] and usb.fake.deleted == []


async def test_a_usb_device_missing_from_the_bus_is_unreachable(usb, monkeypatch):
    import spoolsteward.cups_client as cc
    monkeypatch.undo()                                            # the real is_online: usb:// is judged by the bus listing
    assert await cc.is_online(BROTHER, {BROTHER}) and not await cc.is_online(BROTHER, set()) and await cc.is_online(BROTHER, None)


async def test_page_shows_the_countdown_and_a_delete_button_for_unreachable_queues(usb):
    usb.state["down"] = {BRIDGE, "ipp://192.0.2.20:631/ipp/print"}
    await discovery.run_once(usb.app)
    page = (await usb.get("/admin/printers")).text
    assert 'class="countdown"' in page and page.count('class="countdown"') == 1          # the USB queue only
    assert "/admin/printers/hp/delete" in page and "/admin/printers/lan/delete" in page  # both can be deleted by hand
    usb.state["down"] = set()
    usb.app.state.printers_cache = None
    page = (await usb.get("/admin/printers")).text
    assert "/admin/printers/hp/delete" not in page and 'class="countdown"' not in page


async def test_manual_delete_only_of_queues_that_do_not_answer(usb):
    tok = await csrf_of_page(usb, "/admin/printers")
    await usb.post("/admin/printers/lan/delete", data={"csrf_token": tok})
    assert usb.fake.deleted == []                                                      # it answers: refused
    usb.state["down"] = {"ipp://192.0.2.20:631/ipp/print"}
    usb.app.state.printers_cache = None
    await usb.post("/admin/printers/lan/delete", data={"csrf_token": tok})
    assert usb.fake.deleted == ["lan"]


async def test_only_usb_devices_are_considered_and_the_filter_is_asked_the_way_cups_understands(usb):
    usb.fake.devices = [(BROTHER, "Brother", "DCP-7055")]
    for _ in range(2):
        out = await discovery.run_once(usb.app)
    assert out["notes"] == []                                       # no "Unknown / Virtual PDF printer has no driver" noise
    assert usb.app.state.usb_present == {BROTHER}
    assert [a[1] for a in usb.fake.added] == [BROTHER]


async def test_a_queue_that_is_stopped_still_counts_as_existing(usb):
    usb.fake.stopped = {"hp"}                                       # CUPS stops/disables a queue whose printer was unplugged
    usb.state["bridge"] = [bridge_printer(1, "HP", "Smart Tank 7300")]
    usb.state["bridge"][0]["uri"] = BRIDGE
    assert (await discovery.run_once(usb.app))["created"] == 0 and usb.fake.added == []


async def test_a_printer_that_ipp_usb_serves_is_not_taken_while_the_bridge_restarts(usb):
    hp = "usb://HP/Smart%20Tank%207300%20series?serial=X&interface=1"
    usb.fake.devices = [(hp, "HP", "Smart Tank 7300 series")]
    usb.fake.ppds = usb.fake.ppds + [("gutenprint.5.3://hp/expert", "HP", "Smart Tank 7300 series")]
    usb.state["bridge"] = []                                        # ipp-usb is restarting: nothing on the bridge yet
    assert (await discovery.run_once(usb.app))["created"] == 0
    usb.state["bridge"] = [bridge_printer(60007, "HP", "Smart Tank 7300 series")]   # it is back
    await discovery.run_once(usb.app)
    assert [a[1] for a in usb.fake.added] == ["ipp://ipp-usb:60007/ipp/print"]       # only the driverless queue
    await discovery.run_once(usb.app)
    assert len(usb.fake.added) == 1


async def test_the_same_address_spelled_http_or_ipp_is_one_printer(usb):
    """The bridge is asked over http://, a queue is made with ipp://: they must be recognised as the same device."""
    assert discovery._norm_uri("http://ipp-usb:60001/ipp/print") == discovery._norm_uri(BRIDGE)
    assert discovery._norm_uri("ipp://ipp-usb:60001/ipp/print") != discovery._norm_uri("ipp://ipp-usb:60002/ipp/print")
    assert discovery._norm_uri(BROTHER) != discovery._norm_uri(BRIDGE)


async def test_the_probe_reports_the_ipp_form_of_the_address(usb, monkeypatch):
    import httpx as _h
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        ok = codec.IppMessage((2, 0), 0, 1, [codec.Group(codec.TAG_OPERATION, [
            codec.Attr("attributes-charset", [codec.Val(codec.TAG_CHARSET, "utf-8")]),
            codec.Attr("attributes-natural-language", [codec.Val(codec.TAG_NATURAL_LANG, "en")])]),
            codec.Group(codec.TAG_PRINTER, [codec.Attr("printer-make-and-model", [codec.Val(codec.TAG_TEXT, "HP Smart Tank")]),
                                            codec.Attr("printer-device-id", [codec.Val(codec.TAG_TEXT, "MFG:HP;MDL:Smart Tank;")])])])
        return _h.Response(200, content=codec.encode(ok), headers={"content-type": "application/ipp"})
    import asyncio
    async with _h.AsyncClient(transport=_h.MockTransport(handler)) as c:
        p = await discovery._ask_bridge(c, asyncio.Semaphore(1), 60001)
    assert seen["url"] == "http://ipp-usb:60001/ipp/print" and p["uri"] == BRIDGE
    assert discovery._norm_uri(p["uri"]) == discovery._norm_uri(BRIDGE)


async def test_a_refused_device_is_not_asked_again_at_once(usb, monkeypatch):
    calls = []

    async def refuse(app, name, uri, info, ppd):
        calls.append(uri)
        return False
    monkeypatch.setattr(discovery, "add_queue", refuse)
    usb.state["bridge"] = [bridge_printer(60005, "Epson", "L3150")]
    for _ in range(3):
        out = await discovery.run_once(usb.app)
    assert len(calls) == 1 and out["notes"] == [{"kind": "refused", "model": "Epson L3150"}]   # one try, the message stays
    usb.app.state.discovery_refused.clear()
    await discovery.run_once(usb.app)
    assert len(calls) == 2


async def test_ink_levels_come_from_the_printer_when_the_new_queue_has_none(usb, monkeypatch):
    """CUPS learns the markers only after printing on a queue; a queue just created shows no ink, the printer knows."""
    import spoolsteward.admin.printers as printers_mod

    async def device(base):
        return {"ipp": {"ink": [{"name": "Black", "level": 71, "color": "#000000", "low": False}]}, "usage": [], "config": {}, "scanner": None}
    monkeypatch.setattr(printers_mod, "_device", device)
    page = (await usb.get("/admin/printers")).text
    assert "Ink left" in page
    assert "71%" in page


def test_ink_is_read_from_marker_attributes():
    from spoolsteward.admin.printers import _ink
    g = codec.Group(codec.TAG_PRINTER, [codec.Attr("marker-names", [codec.Val(codec.TAG_NAME, "Black"), codec.Val(codec.TAG_NAME, "Cyan")]),
                                        codec.Attr("marker-levels", [codec.Val(codec.TAG_INTEGER, 5), codec.Val(codec.TAG_INTEGER, 80)]),
                                        codec.Attr("marker-colors", [codec.Val(codec.TAG_NAME, "#000000"), codec.Val(codec.TAG_NAME, "#00ffff")]),
                                        codec.Attr("marker-low-levels", [codec.Val(codec.TAG_INTEGER, 10), codec.Val(codec.TAG_INTEGER, 10)])])
    assert _ink(g) == [{"name": "Black", "level": 5, "color": "#000000", "low": True}, {"name": "Cyan", "level": 80, "color": "#00ffff", "low": False}]
    assert _ink(codec.Group(codec.TAG_PRINTER, [])) == []


# ---------------------------------------------------------------- short names, wiring badge, manual order

@pytest.mark.parametrize("model, expected", [
    ("HP Smart Tank 7300 series", "HP_Smart_Tank_7300"),
    ("HP Smart Tank 7300 series [A7E76E]", "HP_Smart_Tank_7300"),
    ("Hewlett-Packard LaserJet Pro MFP M428fdw", "HP_LaserJet_Pro_M428fdw"),
    ("Brother DCP-7055, using brlaser v6", "Brother_DCP-7055"),
    ("Canon PIXMA TS3350 series", "Canon_PIXMA_TS3350"),
    ("EPSON WorkForce Pro WF-C5790 Series Wireless", "EPSON_WF-C5790"),
    ("§§", "printer"),
])
def test_queue_names_are_short_and_meaningful(model, expected):
    assert discovery.queue_name(model, set()) == expected
    assert len(discovery.queue_name(model, set())) <= discovery.QUEUE_NAME_MAX


def test_a_very_long_model_is_cut_and_names_stay_unique():
    n = discovery.queue_name("Some Maker Ultra Extra Long Product Name Without Any Code", set())
    assert len(n) <= discovery.QUEUE_NAME_MAX and not n.endswith(("_", "-", "."))
    assert discovery.queue_name("HP Smart Tank 7300 series", {"hp_smart_tank_7300"}) == "HP_Smart_Tank_7300_2"


def test_how_a_printer_is_wired():
    assert discovery.transport("ipp://ipp-usb:60000/ipp/print") == "IPP-USB" and discovery.transport(BROTHER) == "USB"
    assert discovery.transport("ipp://192.0.2.20:631/ipp/print") is None and discovery.transport("cups-pdf:/") is None


async def test_cards_carry_the_wiring_badge(usb):
    usb.fake.queues.append(("Brother_DCP-7055", BROTHER))
    page = (await usb.get("/admin/printers")).text
    assert page.count(">IPP-USB<") == 1 and page.count(">USB<") == 1


def _order(page):
    return re.findall(r'<b class="mono">([^<]+)</b>\s*\n', page)


def _all_audits(usb):
    with usb.sm() as s:
        return list(s.scalars(select(AdminAudit)))


async def _as(usb, name):
    await usb.post("/logout", data={"csrf_token": await csrf_of_page(usb, "/admin/printers")})
    await portal_login(usb, name, GOOD)


async def test_printers_can_be_moved_up_and_down_within_their_group(usb):
    usb.fake.queues = [("a", "ipp://192.0.2.1/ipp/print"), ("b", "ipp://192.0.2.2/ipp/print"), ("c", "ipp://192.0.2.3/ipp/print"), ("v", "cups-pdf:/")]
    page = (await usb.get("/admin/printers")).text
    assert _order(page) == ["a", "b", "c", "v"]
    assert re.search(r'/admin/printers/a/move.*?value="up".*?<button class="icon" disabled', page, re.S)       # the first can not go up
    assert re.search(r'/admin/printers/v/move.*?value="down".*?<button class="icon" disabled', page, re.S)    # the last can not go down
    tok = await csrf_of_page(usb, "/admin/printers")
    await usb.post("/admin/printers/c/move", data={"csrf_token": tok, "direction": "up"})
    assert _order((await usb.get("/admin/printers")).text) == ["a", "c", "b", "v"]
    await usb.post("/admin/printers/a/move", data={"csrf_token": tok, "direction": "down"})
    assert _order((await usb.get("/admin/printers")).text) == ["c", "a", "b", "v"]
    # at the end of the group nothing happens (and a virtual printer never passes a physical one)
    await usb.post("/admin/printers/b/move", data={"csrf_token": tok, "direction": "down"})
    await usb.post("/admin/printers/c/move", data={"csrf_token": tok, "direction": "up"})
    assert _order((await usb.get("/admin/printers")).text) == ["c", "a", "b", "v"]
    assert ("printer.move", "boss") in [(a.action, a.actor_name) for a in _all_audits(usb)]


async def test_every_administrator_has_their_own_order(usb):
    usb.fake.queues = [("a", "ipp://192.0.2.1/ipp/print"), ("b", "ipp://192.0.2.2/ipp/print"), ("v", "cups-pdf:/")]
    tok = await csrf_of_page(usb, "/admin/printers")
    await usb.post("/admin/printers/b/move", data={"csrf_token": tok, "direction": "up"})
    assert _order((await usb.get("/admin/printers")).text) == ["b", "a", "v"]
    assert [q for q in re.findall(r'value="([^"]+)"\s+(?:checked\s+)?required', (await usb.get("/me/print")).text)] == ["b", "a", "v"]   # their print form too
    await _as(usb, "boss2")
    assert _order((await usb.get("/admin/printers")).text) == ["a", "b", "v"]                                  # the other one is not affected
    assert [q for q in re.findall(r'value="([^"]+)"\s+(?:checked\s+)?required', (await usb.get("/me/print")).text)] == ["a", "b", "v"]
    tok = await csrf_of_page(usb, "/admin/printers")
    await usb.post("/admin/printers/a/move", data={"csrf_token": tok, "direction": "down"})                       # boss2 puts b first too...
    assert _order((await usb.get("/admin/printers")).text) == ["b", "a", "v"]
    await usb.post("/admin/printers/a/move", data={"csrf_token": tok, "direction": "up"})                         # ...and then changes their mind
    assert _order((await usb.get("/admin/printers")).text) == ["a", "b", "v"]
    await _as(usb, "boss")
    assert _order((await usb.get("/admin/printers")).text) == ["b", "a", "v"]                                  # boss kept theirs


async def test_a_new_printer_goes_after_the_ones_already_placed(usb):
    usb.fake.queues = [("a", "ipp://192.0.2.1/ipp/print"), ("b", "ipp://192.0.2.2/ipp/print")]
    tok = await csrf_of_page(usb, "/admin/printers")
    await usb.post("/admin/printers/b/move", data={"csrf_token": tok, "direction": "up"})
    usb.fake.queues.append(("0new", "ipp://192.0.2.3/ipp/print"))                                             # sorts first by name
    usb.app.state.printers_cache = None
    assert _order((await usb.get("/admin/printers")).text) == ["b", "a", "0new"]


async def test_deleting_a_user_forgets_their_order(usb):
    from spoolsteward.auth.users import delete_user
    from spoolsteward.db.models import PrinterOrder, User
    usb.fake.queues = [("a", "ipp://192.0.2.1/ipp/print"), ("b", "ipp://192.0.2.2/ipp/print")]
    tok = await csrf_of_page(usb, "/admin/printers")
    await usb.post("/admin/printers/b/move", data={"csrf_token": tok, "direction": "up"})
    with usb.sm() as s:
        assert s.scalars(select(PrinterOrder)).first() is not None
        delete_user(s, s.scalars(select(User).where(User.username == "boss")).one())
        s.commit()
        assert s.scalars(select(PrinterOrder)).first() is None


# ---------------------------------------------------------------- rename

async def test_a_queue_can_be_renamed_and_what_the_gateway_keeps_follows(usb):
    from spoolsteward.db.models import PrinterControl, PrinterKeepalive, PrinterOrder, PrinterSeen
    tok = await csrf_of_page(usb, "/admin/printers")
    await usb.post("/admin/printers/lan/move", data={"csrf_token": tok, "direction": "up"})
    with usb.sm() as s:
        s.add(PrinterControl(printer="lan", blocked=True))
        s.add(PrinterKeepalive(printer="lan", enabled=True, interval_hours=24))
        s.commit()
    r = await usb.post("/admin/printers/lan/rename", data={"csrf_token": tok, "new_name": "Office_1"})
    assert r.status_code == 303
    assert usb.fake.added[-1][:3] == ("Office_1", "ipp://192.0.2.20:631/ipp/print", "everywhere") and usb.fake.deleted == ["lan"]
    assert sorted(q[0] for q in usb.fake.queues) == ["Office_1", "hp"]
    with usb.sm() as s:
        for model in (PrinterControl, PrinterKeepalive, PrinterSeen, PrinterOrder):
            names = {r.printer for r in s.scalars(select(model))}
            assert "lan" not in names and (not names or "office_1" in names), model
        assert s.get(PrinterControl, "office_1").blocked
    assert ("printer.rename", "boss") in [(a.action, a.actor_name) for a in _all_audits(usb)]
    page = (await usb.get("/admin/printers")).text
    assert "Office_1" in page and "/admin/printers/lan/" not in page


@pytest.mark.parametrize("new, why", [("bad name!", "letters"), ("hp", "exists"), ("HP", "exists"), ("lan", "same")])
async def test_rename_refuses_bad_or_taken_names(usb, new, why):
    tok = await csrf_of_page(usb, "/admin/printers")
    await usb.post("/admin/printers/lan/rename", data={"csrf_token": tok, "new_name": new})
    assert usb.fake.added == [] and usb.fake.deleted == []


async def test_rename_is_refused_with_jobs_waiting_or_for_an_unknown_driver(usb):
    tok = await csrf_of_page(usb, "/admin/printers")
    usb.fake.jobs = {"lan": 2}
    await usb.post("/admin/printers/lan/rename", data={"csrf_token": tok, "new_name": "x1"})
    usb.fake.jobs = {}
    usb.fake.queues.append(("raw", "socket://192.0.2.30:9100"))
    await usb.post("/admin/printers/raw/rename", data={"csrf_token": tok, "new_name": "x2"})
    assert usb.fake.added == [] and usb.fake.deleted == []


async def test_if_cups_refuses_the_new_queue_the_old_one_stays(usb):
    tok = await csrf_of_page(usb, "/admin/printers")
    usb.fake.refuse_add = True
    await usb.post("/admin/printers/lan/rename", data={"csrf_token": tok, "new_name": "Office_1"})
    assert usb.fake.deleted == [] and [q[0] for q in usb.fake.queues] == ["hp", "lan"]


async def test_a_usb_queue_keeps_its_driver_when_renamed(usb):
    usb.fake.queues.append(("Brother_DCP-7055", BROTHER))
    usb.fake.devices = [(BROTHER, "Brother", "DCP-7055")]
    tok = await csrf_of_page(usb, "/admin/printers")
    await usb.post("/admin/printers/Brother_DCP-7055/rename", data={"csrf_token": tok, "new_name": "Brother_7055"})
    assert usb.fake.added[-1][:3] == ("Brother_7055", BROTHER, "drv:///brlaser.drv/br7055.ppd") and usb.fake.deleted == ["Brother_DCP-7055"]


async def test_the_page_offers_rename_with_the_warning(usb):
    page = (await usb.get("/admin/printers")).text
    assert "/admin/printers/hp/rename" in page and 'name="new_name"' in page
    assert "data-confirm=" in page and "confirm(" in (await usb.get("/admin/static/js/app.js")).text   # the warning before the rename


async def test_move_refuses_bad_input(usb):
    tok = await csrf_of_page(usb, "/admin/printers")
    assert (await usb.post("/admin/printers/a/move", data={"csrf_token": tok, "direction": "sideways"})).status_code == 404
    assert (await usb.post("/admin/printers/nope/move", data={"csrf_token": tok, "direction": "up"})).status_code == 303
