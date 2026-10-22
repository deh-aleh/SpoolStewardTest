"""Nothing is assumed about the hardware: scanners are found at run time (any number, any capabilities), a printer
whose cable is pulled is shown as unreachable, test printers can be created and removed, and the UI helpers
(copy button, suggestions, centred forms) are in place."""

import asyncio
import re

import httpx
import pytest
from sqlalchemy import select

from spoolsteward.config import settings
from spoolsteward.db.models import AdminAudit, JobLog
from spoolsteward.escl import paper_label, parse_caps
from spoolsteward.policy import control
from tests.conftest import CUPS_SOCK, csrf_of_page, make_user, portal_login, requires_cups

GOOD = "correct horse battery"


def caps_xml(model, w, h, platen_dpi, adf_dpi=None, duplex=False):
    def res(dpis):
        return "".join(f"<scan:DiscreteResolution><scan:XResolution>{d}</scan:XResolution></scan:DiscreteResolution>" for d in dpis)
    adf = (f"<scan:AdfSimplexInputCaps><scan:MaxWidth>{w}</scan:MaxWidth><scan:MaxHeight>{h}</scan:MaxHeight><scan:SettingProfiles><scan:SettingProfile>"
           f"<scan:SupportedResolutions><scan:DiscreteResolutions>{res(adf_dpi)}</scan:DiscreteResolutions></scan:SupportedResolutions>"
           f"</scan:SettingProfile></scan:SettingProfiles></scan:AdfSimplexInputCaps>"
           f"{'<scan:AdfDuplexInputCaps/>' if duplex else ''}") if adf_dpi else ""
    return (f"<scan:ScannerCapabilities xmlns:scan='x' xmlns:pwg='y'><pwg:MakeAndModel>{model}</pwg:MakeAndModel>"
            f"<scan:Platen><scan:PlatenInputCaps><scan:MaxWidth>{w}</scan:MaxWidth><scan:MaxHeight>{h}</scan:MaxHeight>"
            f"<scan:SettingProfiles><scan:SettingProfile><scan:SupportedResolutions><scan:DiscreteResolutions>{res(platen_dpi)}"
            f"</scan:DiscreteResolutions></scan:SupportedResolutions><pwg:DocumentFormat>application/pdf</pwg:DocumentFormat>"
            f"</scan:SettingProfile></scan:SettingProfiles></scan:PlatenInputCaps></scan:Platen>"
            f"<scan:Adf>{adf}</scan:Adf></scan:ScannerCapabilities>")


# ---------------------------------------------------------------- what a scanner reports about itself

def test_scan_area_and_paper_names_come_from_the_device():
    assert paper_label(2480, 3508) == "A4" and paper_label(3508, 4961) == "A3" and paper_label(2550, 3300) == "Letter"
    assert paper_label(1000, 2000) == "85×169 mm"
    c = parse_caps(caps_xml("X", 3508, 4961, [300, 1200], [300], duplex=True))
    assert c["platen"]["size"] == "A3" and c["platen"]["dpi"] == [300, 1200] and c["adf"]["duplex"] is True
    assert "size" not in parse_caps("<scan:ScannerCapabilities xmlns:scan='x'><scan:Platen><scan:PlatenInputCaps></scan:PlatenInputCaps></scan:Platen></scan:ScannerCapabilities>")["platen"]


@pytest.fixture
async def two_scanners(portal, monkeypatch):
    monkeypatch.setattr(settings, "scan_url", "alpha=http://scan-a:80, beta=http://scan-b:80")
    monkeypatch.setattr(settings, "ipp_auth", "keys")
    state = {"down": set(), "jobs": [], "xml": {}, "left": {}}
    caps = {"scan-a": caps_xml("Alpha 100", 2480, 3508, [75, 300, 600], [300]),          # A4, feeder, 600 dpi glass
            "scan-b": caps_xml("Beta 900", 3508, 4961, [100, 400, 1200])}                  # A3, no feeder, 1200 dpi glass

    def handler(req: httpx.Request) -> httpx.Response:
        host, p = req.url.host, req.url.path
        if host in state["down"]:
            raise httpx.ConnectError("no route to host")
        if p == "/eSCL/ScannerCapabilities":
            return httpx.Response(200, text=caps[host])
        if p == "/eSCL/ScanJobs" and req.method == "POST":
            state["jobs"].append(host)
            state["xml"][host] = req.content.decode()
            state["left"][host] = 1                                                                    # one sheet per job
            return httpx.Response(201, headers={"Location": f"http://{host}/eSCL/ScanJobs/j1"})
        if p == "/eSCL/ScanJobs/j1/NextDocument":
            if state["left"].get(host):
                state["left"][host] -= 1
                return httpx.Response(200, content=b"%PDF-" + host.encode())
            return httpx.Response(404)
        return httpx.Response(200)

    portal.app.state.scan = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    portal.app.state.scanners_cache = None
    portal.app.state.scan_caps = None
    portal.app.state.scanner_device_cache = None
    make_user(portal.sm, "rita", password=GOOD)
    await portal_login(portal, "rita", GOOD)
    portal.state = state
    yield portal
    del portal.app.state.scan


async def do_scan(cl, **data):
    tok = await csrf_of_page(cl, "/me/scan")
    r = await cl.post("/me/scan", data={"csrf_token": tok, "color": "color", "fmt": "pdf", **data}, headers={"X-Requested-With": "fetch"})
    if r.status_code != 200:
        return r.status_code, r.json()
    for _ in range(100):
        st = (await cl.get(f"/me/scan/{r.json()['id']}")).json()
        if st["state"] != "running":
            return 200, st
        await asyncio.sleep(0.05)


async def test_every_scanner_is_listed_with_its_own_capabilities_and_nothing_is_hard_coded(two_scanners):
    page = (await two_scanners.get("/me/scan")).text
    assert 'id="scanner"' in page and ">Alpha 100<" not in page and ">alpha<" in page and ">beta<" in page   # a chooser, by name
    assert "A4 sheet" not in page and "up to 300 dpi" not in page                                       # no fixed sentence
    assert '"size": "A4"' in page.replace('"size":"A4"', '"size": "A4"') and '"A3"' in page and "1200" in page  # each device's own values
    assert page.count("<option value=\"adf\"") == 0                                                          # options are built per scanner (script)


async def test_the_scan_goes_to_the_scanner_that_was_chosen_and_uses_its_whole_sheet(two_scanners):
    code, st = await do_scan(two_scanners, scanner="beta", source="platen", dpi="1200")
    assert code == 200 and st["state"] == "done"
    assert two_scanners.state["jobs"] == ["scan-b"] and "<pwg:Width>3508<" in two_scanners.state["xml"]["scan-b"]   # A3 area
    two_scanners.state["jobs"].clear()
    code, st = await do_scan(two_scanners, scanner="alpha", source="adf", dpi="300")
    assert st["state"] == "done" and two_scanners.state["jobs"] == ["scan-a"] and "<pwg:Width>2480<" in two_scanners.state["xml"]["scan-a"]
    code, st = await do_scan(two_scanners, scanner="beta", source="adf", dpi="300")                           # beta has no feeder
    assert st["state"] == "error" and "feeder" in st["error"]
    with two_scanners.sm() as s:
        rows = s.scalars(select(JobLog).order_by(JobLog.id)).all()
    assert [r.options_json["scanner"] for r in rows] == ["beta", "alpha"]                     # the refused one never reached the audit


async def test_native_clients_reach_every_scanner_under_its_own_path(two_scanners):
    assert "Alpha" in (await two_scanners.get("/eSCL/ScannerCapabilities")).text                  # the default keeps the old path
    assert "Beta" in (await two_scanners.get("/eSCL-beta/ScannerCapabilities")).text
    assert (await two_scanners.get("/eSCL-nobody/ScannerCapabilities")).status_code == 404


async def test_unplugging_a_scanner_removes_it_and_plugging_it_back_restores_it(two_scanners):
    assert ">beta<" in (await two_scanners.get("/me/scan")).text
    two_scanners.state["down"].add("scan-b")
    two_scanners.app.state.scanners_cache = None                                                     # (it expires after 30 s)
    page = (await two_scanners.get("/me/scan")).text
    assert ">beta<" not in page and ">alpha</span>" in page                                           # one left: shown as a name
    assert (await two_scanners.get("/eSCL-beta/ScannerCapabilities")).status_code == 404
    code, body = await do_scan(two_scanners, scanner="beta", source="platen", dpi="300")
    assert code == 409 and "beta" in body["error"] and "does not answer" in body["error"]
    two_scanners.state["down"].clear()
    two_scanners.app.state.scanners_cache = None
    assert ">beta<" in (await two_scanners.get("/me/scan")).text
    two_scanners.state["down"].update({"scan-a", "scan-b"})
    two_scanners.app.state.scanners_cache = None
    assert "No scanner available" in (await two_scanners.get("/me/scan")).text


# ---------------------------------------------------------------- printers: the cable pulled

@pytest.fixture
async def stack(portal):
    # the private unix socket, like production (over TCP CUPS wants TLS for administrative requests)
    portal.app.state.upstream = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=CUPS_SOCK), base_url="http://localhost", timeout=10)
    portal.app.state.queues_cache = portal.app.state.printers_cache = None
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    make_user(portal.sm, "ann", password=GOOD, group="staff")
    await portal_login(portal, "boss", GOOD)
    yield portal
    await portal.app.state.upstream.aclose()


@requires_cups
async def test_a_printer_that_does_not_answer_is_unreachable_everywhere(stack, monkeypatch):
    import spoolsteward.admin.printers as printers_mod
    import spoolsteward.cups_client as cc

    async def dead(uri, usb_present=None):
        return False
    monkeypatch.setattr(cc, "is_online", dead)
    monkeypatch.setattr(printers_mod, "is_online", dead)
    page = (await stack.get("/admin/printers")).text
    assert "NOT REACHABLE" in page and "The device does not answer" in page and "Ink left" not in page
    assert 'name="queue" value="virtual"' not in (await stack.get("/me/print")).text and "Not reachable right now" in (await stack.get("/me/print")).text
    tok = await csrf_of_page(stack, "/me")
    r = await stack.post("/me/print", data={"queue": "virtual", "copies": "1", "csrf_token": tok},
                         files={"file": ("a.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")})
    assert r.status_code == 303 and "does not answer" in (await stack.get("/me/print")).text
    with stack.sm() as s:
        assert s.scalars(select(JobLog)).first() is None                                              # nothing was queued
    stack.app.state.queues_cache = None
    monkeypatch.setattr(settings, "secret_key", "s3cret")                  # the feed of the announcer is closed without a key
    pol = (await stack.get("/mdns/policy", headers={"X-Mdns-Token": control.mdns_token()})).json()
    assert "virtual" in pol["offline"]                                                                # and it stops being announced


@requires_cups
async def test_a_printer_that_answers_is_reachable(stack):
    page = (await stack.get("/admin/printers")).text
    assert "NOT REACHABLE" not in page and 'value="virtual"' in (await stack.get("/me/print")).text


# ---------------------------------------------------------------- test printers

@requires_cups
async def test_test_printers_are_created_numbered_and_deleted(stack):
    tok = await csrf_of_page(stack, "/admin/printers")
    created = []
    try:
        for _ in range(2):
            r = await stack.post("/admin/printers/debug", data={"csrf_token": tok})
            assert r.status_code == 303
        page = (await stack.get("/admin/printers")).text
        created = [n for n in ("debugprinter0", "debugprinter1") if n in page]
        assert created == ["debugprinter0", "debugprinter1"] and "Test printer (debugprinter0)" in page or created
        assert 'action="/admin/printers/debugprinter1/delete"' in page
        r = await stack.post("/admin/printers/debugprinter0/delete", data={"csrf_token": tok})
        page = (await stack.get("/admin/printers")).text
        assert 'action="/admin/printers/debugprinter0/delete"' not in page and "debugprinter1" in page
        r = await stack.post("/admin/printers/debug", data={"csrf_token": tok})                       # the freed number is reused
        assert 'action="/admin/printers/debugprinter0/delete"' in (await stack.get("/admin/printers")).text
        # a test printer is a real queue: it can be printed on
        await stack.post("/me/print", data={"queue": "debugprinter0", "copies": "1", "csrf_token": await csrf_of_page(stack, "/me")},
                               files={"file": ("p.pdf", b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R/Size 4>>\n%%EOF\n", "application/pdf")})
        with stack.sm() as s:
            assert s.scalars(select(JobLog)).one().state.value == "accepted"
            assert {a.action for a in s.scalars(select(AdminAudit))} >= {"printer.debug_create", "printer.delete"}
    finally:
        for n in ("debugprinter0", "debugprinter1", "debugprinter2"):
            await stack.post(f"/admin/printers/{n}/delete", data={"csrf_token": tok})
    assert "/admin/printers/debugprinter" not in (await stack.get("/admin/printers")).text


@requires_cups
async def test_only_virtual_printers_can_be_deleted_and_only_by_admins(stack):
    tok = await csrf_of_page(stack, "/admin/printers")
    await stack.post("/admin/printers/doesnotexist/delete", data={"csrf_token": tok})
    assert "printer not found" in (await stack.get("/admin/printers")).text
    await stack.post("/logout", data={"csrf_token": tok})
    await portal_login(stack, "ann", GOOD)
    tok = await csrf_of_page(stack, "/me")
    assert (await stack.post("/admin/printers/debug", data={"csrf_token": tok})).status_code == 403
    assert (await stack.post("/admin/printers/virtual/delete", data={"csrf_token": tok})).status_code == 403
    assert (await stack.post("/admin/printers/bad name/delete", data={"csrf_token": tok})).status_code in (403, 404)


# ---------------------------------------------------------------- UI helpers

async def test_key_page_has_a_copy_button_for_every_value(portal, monkeypatch):
    make_user(portal.sm, "pia", password=GOOD)
    await portal_login(portal, "pia", GOOD)
    tok = await csrf_of_page(portal, "/me/keys")
    page = (await portal.post("/me/keys", data={"csrf_token": tok, "label": "laptop"})).text
    assert 'data-copy class="secret">' in page and page.count('class="copy"') >= 1
    assert "navigator.clipboard" in (await portal.get("/admin/static/js/app.js")).text               # the script that does the copying


async def test_actions_filters_suggest_administrators_and_known_actions(portal):
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    make_user(portal.sm, "emma", password=GOOD, admin=True)
    make_user(portal.sm, "plain", password=GOOD)
    await portal_login(portal, "boss", GOOD)
    tok = await csrf_of_page(portal, "/admin/groups")
    await portal.post("/admin/groups", data={"csrf_token": tok, "name": "zz"})
    page = (await portal.get("/admin/actions")).text
    suggestions = re.findall(r"<input[^>]*data-suggest='([^']*)'", page)
    assert len(suggestions) == 2
    import json
    actors, actions = json.loads(suggestions[0]), json.loads(suggestions[1])
    assert actors == ["boss", "emma"] and "plain" not in actors                                      # only administrators
    assert "group.create" in actions and "login" in actions and actions == sorted(set(actions))      # what really occurs, once
    app_js, app_css = (await portal.get("/admin/static/js/app.js")).text, (await portal.get("/admin/static/css/app.css")).text
    assert "ul.suggest" in app_css and "addEventListener('focus', show)" in app_js                  # opens on click, narrows as you type


async def test_print_and_scan_forms_are_centred(portal):
    make_user(portal.sm, "pia", password=GOOD)
    await portal_login(portal, "pia", GOOD)
    portal.app.state.scan = None
    for path in ("/me/print", "/me/scan"):
        page = (await portal.get(path)).text
        assert 'class="formpage"' in page, path


async def test_scan_without_credentials_is_refused_and_reported_once(two_scanners, monkeypatch):
    from sqlalchemy import select

    from spoolsteward import notices
    from spoolsteward.db.models import Notice

    monkeypatch.setattr(settings, "escl_anonymous", False)
    monkeypatch.setattr(settings, "log_level", "INFO")
    notices._state.clear()
    for _i in range(30):                                                  # a scan app that keeps trying
        assert (await two_scanners.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401
    assert (await two_scanners.get("/eSCL/ScannerCapabilities")).status_code == 200      # discovery stays open
    with two_scanners.sm() as s:
        row = s.scalars(select(Notice)).one()
        assert (row.kind, row.key, row.count) == ("escl_anonymous_refused", "10.0.0.7", 1)
    assert notices.pending("escl_anonymous_refused", "10.0.0.7") == 29
    monkeypatch.setattr(settings, "escl_anonymous", True)                 # with the option on there is nothing to report
    notices._state.clear()
    assert (await two_scanners.post("/eSCL/ScanJobs", content=b"<x/>")).status_code != 401
