"""eSCL through the gateway: capabilities, scans, limits, guest scans, feeder, resolutions, page ranges."""

import re

import httpx
import pytest
from sqlalchemy import select

from spoolsteward.auth import keys as keylib
from spoolsteward.config import settings
from spoolsteward.db.models import JobKind, JobLog, JobState, Metric, Notice, Period, PolicyRule, SubjectType, User
from tests.conftest import csrf_of_page, make_user, portal_login

GOOD = "correct horse battery"


# ---------------------------------------------------------------- eSCL through the gateway

ESCL_CAPS = "<scan:ScannerCapabilities xmlns:scan='x'><scan:UUID>u</scan:UUID></scan:ScannerCapabilities>"


def fake_scanner():
    state = {"jobs": {}, "busy": False}

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/eSCL/ScannerCapabilities":
            return httpx.Response(200, text=ESCL_CAPS, headers={"content-type": "text/xml"})
        if p == "/eSCL/ScanJobs" and req.method == "POST":
            if state["busy"]:
                return httpx.Response(503, text="busy")
            state["jobs"]["j1"] = 1
            return httpx.Response(201, headers={"Location": "http://ipp-usb:60000/eSCL/ScanJobs/j1"})
        if p == "/eSCL/ScanJobs/j1/NextDocument":
            if state["jobs"].get("j1"):
                state["jobs"]["j1"] = 0
                return httpx.Response(200, content=b"%PDF-fake", headers={"content-type": "application/pdf"})
            return httpx.Response(404)
        if p == "/eSCL/ScanJobs/j1" and req.method == "DELETE":
            return httpx.Response(200)
        return httpx.Response(404)

    return handler, state


@pytest.fixture
async def scanner(portal, monkeypatch):
    monkeypatch.setattr(settings, "ipp_auth", "keys")
    handler, state = fake_scanner()
    portal.app.state.scan = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ipp-usb:60000")
    uid = make_user(portal.sm, "rita", password=GOOD)
    other = make_user(portal.sm, "sam", password=GOOD)
    with portal.sm() as s:
        _, plain = keylib.create_key(s, s.get(User, uid), "pc", 30)
        _, plain2 = keylib.create_key(s, s.get(User, other), "pc", 30)
    portal.state, portal.key, portal.key2 = state, plain, plain2
    yield portal
    del portal.app.state.scan


async def do_scan(cl, tok, **data):
    """The portal scans in the background: start, wait for the job to finish, return (status json, file response)."""
    import asyncio
    r = await cl.post("/me/scan", data={"csrf_token": tok, **data}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    for _ in range(100):
        st = (await cl.get(f"/me/scan/{sid}")).json()
        if st["state"] != "running":
            break
        await asyncio.sleep(0.05)
    return st, (await cl.get(f"/me/scan/{sid}/file")) if st["state"] == "done" else None


def scan_rows(cl):
    with cl.sm() as s:
        return [(r.kind, r.state, r.pages, r.user_name) for r in s.scalars(select(JobLog).order_by(JobLog.id))]


async def test_capabilities_are_public_everything_else_needs_a_key(scanner):
    assert (await scanner.get("/eSCL/ScannerCapabilities")).status_code == 200
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>", auth=("rita", "wrong"))).status_code == 401
    assert (await scanner.delete("/eSCL/ScanJobs/j1")).status_code == 401
    assert (await scanner.get("/eSCL/../../etc/passwd")).status_code in (404, 400)
    assert (await scanner.put("/eSCL/ScanJobs", content=b"x")).status_code in (404, 405)


async def test_full_scan_is_audited_and_the_location_points_at_the_gateway(scanner):
    r = await scanner.post("/eSCL/ScanJobs", content=b"<x/>", auth=("rita", scanner.key))
    assert r.status_code == 201 and r.headers["location"] == "https://gw.test/eSCL/ScanJobs/j1"      # not ipp-usb
    d = await scanner.get("/eSCL/ScanJobs/j1/NextDocument", auth=("rita", scanner.key))
    assert d.status_code == 200 and d.content == b"%PDF-fake"
    assert scan_rows(scanner) == [(JobKind.scan, JobState.completed, 1, "rita")]
    assert (await scanner.get("/eSCL/ScanJobs/j1/NextDocument", auth=("rita", scanner.key))).status_code == 404
    await scanner.delete("/eSCL/ScanJobs/j1", auth=("rita", scanner.key))
    assert scan_rows(scanner)[0][1] is JobState.completed                  # a finished scan is not "canceled" afterwards


async def test_another_user_cannot_fetch_or_cancel_your_scan(scanner):
    await scanner.post("/eSCL/ScanJobs", content=b"<x/>", auth=("rita", scanner.key))
    assert (await scanner.get("/eSCL/ScanJobs/j1/NextDocument", auth=("sam", scanner.key2))).status_code == 404
    assert (await scanner.delete("/eSCL/ScanJobs/j1", auth=("sam", scanner.key2))).status_code == 404
    assert scan_rows(scanner) == [(JobKind.scan, JobState.accepted, None, "rita")]


async def test_a_busy_scanner_is_reported_and_leaves_no_phantom_quota(scanner):
    scanner.state["busy"] = True
    r = await scanner.post("/eSCL/ScanJobs", content=b"<x/>", auth=("rita", scanner.key))
    assert r.status_code == 503
    assert scan_rows(scanner)[0][1] is JobState.failed


async def test_scan_limits_apply(scanner):
    with scanner.sm() as s:
        rid = s.scalars(select(User).where(User.username == "rita")).one().id
        s.add(PolicyRule(subject_type=SubjectType.user, subject_id=rid, resource="scan", metric=Metric.jobs, limit=1, period=Period.weekly))
        s.commit()
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>", auth=("rita", scanner.key))).status_code == 201
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>", auth=("rita", scanner.key))).status_code == 503


async def test_browser_scan_returns_the_file_and_audits_it(scanner):
    await portal_login(scanner, "rita", GOOD)
    assert "Scan" in (await scanner.get("/me/scan")).text
    tok = await csrf_of_page(scanner, "/me/scan")
    st, r = await do_scan(scanner, tok, source="platen", color="color", dpi="300", fmt="pdf")
    assert r.status_code == 200 and r.content == b"%PDF-fake" and "attachment" in r.headers["content-disposition"]
    assert scan_rows(scanner)[0][:2] == (JobKind.scan, JobState.completed)
    bad = await scanner.post("/me/scan", data={"csrf_token": tok, "source": "x", "color": "color", "dpi": "300", "fmt": "pdf"})
    assert bad.status_code == 400


def test_scanner_is_announced_with_escl_keys():
    from spoolsteward.mdns import scanner_txt
    t = scanner_txt({"model": "HP Scan", "uuid": "abc", "adf": True, "color": True}, "https://h/me")
    assert t["rs"] == "eSCL" and t["is"] == "platen,adf" and "color" in t["cs"] and t["UUID"] == "abc"


# ---------------------------------------------------------------- M4: guest scans, feeder, resolutions, page ranges

FULL_CAPS = """<scan:ScannerCapabilities xmlns:scan='x' xmlns:pwg='y'>
<scan:Platen><scan:PlatenInputCaps><scan:SettingProfiles><scan:SettingProfile><scan:SupportedResolutions><scan:DiscreteResolutions>
%s</scan:DiscreteResolutions></scan:SupportedResolutions><pwg:DocumentFormat>application/pdf</pwg:DocumentFormat>
</scan:SettingProfile></scan:SettingProfiles></scan:PlatenInputCaps></scan:Platen>
<scan:Adf><scan:AdfSimplexInputCaps><scan:SettingProfiles><scan:SettingProfile><scan:SupportedResolutions><scan:DiscreteResolutions>
%s</scan:DiscreteResolutions></scan:SupportedResolutions></scan:SettingProfile></scan:SettingProfiles></scan:AdfSimplexInputCaps></scan:Adf>
</scan:ScannerCapabilities>"""


def _res(*dpis):
    return "".join(f"<scan:DiscreteResolution><scan:XResolution>{d}</scan:XResolution><scan:YResolution>{d}</scan:YResolution></scan:DiscreteResolution>" for d in dpis)


def _pdf_page() -> bytes:
    import io
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(100, 100)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def test_each_source_reports_its_own_resolutions():
    from spoolsteward.escl import parse_caps
    caps = parse_caps(FULL_CAPS % (_res(75, 300, 600, 1200), _res(75, 300)))
    assert caps["platen"]["dpi"] == [75, 300, 600, 1200] and caps["adf"]["dpi"] == [75, 300]


def test_page_ranges_are_parsed_and_counted():
    from spoolsteward.policy.pages import pages_in_ranges, parse_ranges
    assert parse_ranges("1, 5") == [(1, 1), (5, 5)] and parse_ranges("2-4,7") == [(2, 4), (7, 7)] and parse_ranges("") == []
    assert pages_in_ranges(parse_ranges("1,5"), 10) == 2 and pages_in_ranges(parse_ranges("3-"), 10) == 8
    assert pages_in_ranges(parse_ranges("1-3,2-4"), 10) == 4 and pages_in_ranges([], 7) == 7
    assert pages_in_ranges(parse_ranges("1,5"), None) == 2 and pages_in_ranges(parse_ranges("3-"), None) is None
    for bad in ("0", "5-2", "a", "1;2", "1-2-3", "-3"):
        with pytest.raises(ValueError):
            parse_ranges(bad)
    with pytest.raises(ValueError):
        pages_in_ranges(parse_ranges("20"), 10)


async def test_guest_scans_need_the_switch_and_are_audited_under_the_guest_name(scanner, monkeypatch):
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401
    monkeypatch.setattr(settings, "escl_anonymous", True)
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>", auth=("rita", "wrong"))).status_code == 401  # bad creds still fail
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 201
    assert scan_rows(scanner)[0][3] == "unknown-guest"
    with scanner.sm() as s:
        guest = s.scalars(select(User).where(User.username == "unknown-guest")).one()
        assert guest.provider == "local" and not guest.password_hash and not guest.is_admin


async def test_the_guest_name_comes_from_the_setting(scanner, monkeypatch):
    monkeypatch.setattr(settings, "escl_anonymous", True)
    monkeypatch.setattr(settings, "escl_guest_username", "scan-anon")
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 201
    assert scan_rows(scanner)[0][3] == "scan-anon"
    with scanner.sm() as s:
        assert s.scalars(select(User).where(User.username == "unknown-guest")).first() is None


@pytest.fixture(autouse=True)
def _fresh_notice_memory():
    from spoolsteward import notices
    notices._state.clear()      # "reported a moment ago" is kept in memory: one test must not silence the next
    yield
    notices._state.clear()


def guest_notices(cl):
    with cl.sm() as s:
        return list(s.scalars(select(Notice).where(Notice.kind == "escl_guest_name_taken", Notice.user_id.is_(None))))


@pytest.mark.parametrize("who", [dict(password=GOOD), dict(provider="radius"), dict(admin=True)])
async def test_a_real_account_with_the_guest_name_is_never_used(scanner, monkeypatch, caplog, who):
    """Scans would be credited to a real person: refuse, tell the administrators (bell) and write it in the log."""
    monkeypatch.setattr(settings, "escl_anonymous", True)
    make_user(scanner.sm, "unknown-guest", **who)
    with caplog.at_level("WARNING"):
        assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401
    assert scan_rows(scanner) == []                                   # nothing was scanned, nobody was credited
    (n,) = guest_notices(scanner)
    assert n.key == "unknown-guest" and n.dismissed_at is None
    assert any("unknown-guest" in r.getMessage() and "SPOOLSTEWARD_ESCL_GUEST_USERNAME" in r.getMessage() for r in caplog.records)
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401   # again: counted, not a second row
    assert len(guest_notices(scanner)) == 1


async def test_every_administrator_sees_the_guest_notice_and_the_emergency_one_too(scanner, monkeypatch):
    monkeypatch.setattr(settings, "escl_anonymous", True)
    make_user(scanner.sm, "unknown-guest", provider="radius")
    make_user(scanner.sm, "boss", password=GOOD, admin=True)
    make_user(scanner.sm, settings.bootstrap_admin_username, password=GOOD, admin=True)
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401
    for admin in ("boss", settings.bootstrap_admin_username):
        assert (await portal_login(scanner, admin, GOOD)).status_code in (200, 302, 303)
        page = (await scanner.get("/admin/notices")).text
        assert "SPOOLSTEWARD_ESCL_GUEST_USERNAME" in page and "unknown-guest" in page, admin
        await scanner.post("/logout", data={"csrf_token": await csrf_of_page(scanner)})


async def test_the_guest_notice_closes_itself_when_the_name_is_free(scanner, monkeypatch):
    monkeypatch.setattr(settings, "escl_anonymous", True)
    make_user(scanner.sm, "unknown-guest", password=GOOD)
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401
    monkeypatch.setattr(settings, "escl_guest_username", "scan-anon")   # the administrator set another name
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 201
    monkeypatch.setattr(settings, "escl_guest_username", "unknown-guest")
    (n,) = guest_notices(scanner)
    assert n.dismissed_at is None                                        # the old name is still taken: not closed by the other name
    with scanner.sm() as s:
        s.delete(s.scalars(select(User).where(User.username == "unknown-guest")).one())
        s.commit()
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 201
    (n,) = guest_notices(scanner)
    assert n.dismissed_by == "system"


def test_the_guest_name_is_checked_at_start():
    from pydantic import ValidationError

    from spoolsteward.config import Settings
    assert Settings(escl_guest_username="scan-anon").escl_guest_username == "scan-anon"
    for bad in ("Guest Name", "Scan-Anon", "-x", "a" * 64, "ü", "a/b", "", "none", "administrator"):   # the last: the emergency administrator
        with pytest.raises(ValidationError):
            Settings(escl_guest_username=bad)


async def test_feeder_scan_reads_every_sheet_and_the_limit_follows_the_source(scanner):
    pages = [_pdf_page(), _pdf_page(), _pdf_page()]
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/eSCL/ScannerCapabilities":
            return httpx.Response(200, text=FULL_CAPS % (_res(75, 300, 600, 1200), _res(75, 300)))
        if p == "/eSCL/ScannerStatus":
            return httpx.Response(200, text="<scan:ScannerStatus xmlns:scan='x'><scan:AdfState>ScannerAdfLoaded</scan:AdfState></scan:ScannerStatus>")
        if p == "/eSCL/ScanJobs" and req.method == "POST":
            seen["xml"] = req.content.decode()
            return httpx.Response(201, headers={"Location": "http://ipp-usb:60000/eSCL/ScanJobs/j9"})
        if p == "/eSCL/ScanJobs/j9/NextDocument":
            return httpx.Response(200, content=pages.pop(0)) if pages else httpx.Response(404)
        return httpx.Response(200)

    scanner.app.state.scan = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ipp-usb:60000")
    scanner.app.state.scan_caps = None
    await portal_login(scanner, "rita", GOOD)
    html = (await scanner.get("/me/scan")).text
    assert 'name="filename"' in html and "1200" in html                       # glass offers its real maximum
    tok = await csrf_of_page(scanner, "/me/scan")
    st, r = await do_scan(scanner, tok, source="auto", color="color", dpi="300", fmt="pdf", filename="contract/final")
    assert r.status_code == 200 and "<pwg:InputSource>Feeder<" in seen["xml"]                # auto picked the loaded feeder
    assert 'filename="final.pdf"' in r.headers["content-disposition"]   # path part dropped
    assert "/" not in r.headers["content-disposition"].split("filename=")[1].split(";")[0]
    from pypdf import PdfReader
    import io
    assert len(PdfReader(io.BytesIO(r.content)).pages) == 3
    assert scan_rows(scanner)[0][:3] == (JobKind.scan, JobState.completed, 3)
    st, nofile = await do_scan(scanner, tok, source="adf", color="color", dpi="600", fmt="pdf")
    assert st["state"] == "error" and "300 dpi" in st["error"] and nofile is None
    assert "300 dpi" in (await scanner.get("/me/scan")).text                      # the page lists the failure too


async def test_a_scan_survives_leaving_the_page_and_belongs_to_its_owner(scanner):
    await portal_login(scanner, "rita", GOOD)
    tok = await csrf_of_page(scanner, "/me/scan")
    r = await scanner.post("/me/scan", data={"csrf_token": tok, "source": "platen", "color": "color", "dpi": "300", "fmt": "pdf"})
    assert r.status_code == 303                                                  # no-JS form: the result is listed on the page
    import asyncio
    await asyncio.sleep(0.3)
    page = (await scanner.get("/me/scan")).text
    assert "Download" in page and scan_rows(scanner)[0][1] is JobState.completed
    sid = re.search(r"/me/scan/([\w-]+)/file", page).group(1)
    assert (await scanner.get(f"/me/scan/{sid}/file")).content == b"%PDF-fake"
    await scanner.post("/logout", data={"csrf_token": tok})
    await portal_login(scanner, "sam", GOOD)
    assert (await scanner.get(f"/me/scan/{sid}/file")).status_code == 404       # someone else's scan


async def test_guest_scan_records_the_host_name(portal, monkeypatch):
    from spoolsteward import escl

    monkeypatch.setattr(escl.socket, "gethostbyaddr", lambda ip: ("laptop.lan", [], [ip]))
    assert await escl._hostname("10.0.0.7") == "laptop.lan"
    monkeypatch.setattr(escl.socket, "gethostbyaddr", lambda ip: (_ for _ in ()).throw(OSError("no name")))
    assert await escl._hostname("10.0.0.7") == ""


def test_mfp_badge_follows_what_the_device_reports():
    """Only a device that also answers as an eSCL scanner is marked MFP; nothing depends on a model name."""
    from spoolsteward.admin.common import templates

    def render(scanner):
        q = {"name": "q1", "state": "idle", "online": True, "accepting": True, "queued": 0, "reasons": [], "message": "",
             "device_uri": "ipp://x", "ink": [], "location": "", "color": False}
        dev = {"ipp": {}, "usage": [], "config": {}, "scanner": scanner}
        d = {"title": "Printer", "base": "http://x:631", "online": True, "virtual": False, "queues": [q], "device": dev, "stats": {}, "scan_stats": None}
        return templates.get_template("printers.html").render(
            request=type("R", (), {"url": type("U", (), {"path": "/admin/printers"})()})(), me=None, csrf="t", flashes=[],
            devices=[d], controls={}, key=str.lower, maintenance=False, keepalive={}, next_due=lambda r: None,
            describe=str, split=lambda h: (h, "hours"), presets=[], transport=lambda u: None, refresh_seconds=15)

    assert ">MFP<" in render({"state": "Idle"})
    assert ">MFP<" not in render(None)


# ---------------------------------------------------------------- how long and how much of the finished scans of the portal stay in memory

def _held(app, **records):
    """Put finished scans in the memory of the portal: name -> (bytes, seconds ago it finished)."""
    import time
    app.state.web_scans = {n: {"user_id": 1, "state": "done", "name": n, "ts": time.monotonic() - ago, "data": b"x" * size,
                               "ctype": "application/pdf", "ext": "pdf", "pages": 1} for n, (size, ago) in records.items()}
    return app.state.web_scans


def test_the_defaults_are_50_mb_10_minutes_and_keep_after_download():
    from spoolsteward.config import Settings

    s = Settings(_env_file=None)
    assert (s.scan_keep_mb, s.scan_keep_minutes, s.scan_delete_after_download) == (50, 10, False)


async def test_a_finished_scan_is_forgotten_after_the_minutes_in_the_settings(portal, monkeypatch):
    from spoolsteward import escl

    monkeypatch.setattr(settings, "scan_keep_minutes", 10)
    import time
    long_ago = time.monotonic() - 3600     # not 0.0: the monotonic clock starts at boot, and a fresh CI machine is younger than the limit
    d = _held(portal.app, young=(10, 9 * 60), old=(10, 11 * 60))
    d["busy"] = {"user_id": 1, "state": "running", "name": "b", "ts": long_ago}                 # a running scan has no age limit
    d["failed"] = {"user_id": 1, "state": "error", "name": "f", "ts": long_ago, "error": "x"}  # a failed one is forgotten like the others
    assert sorted(escl._scans(portal.app)) == ["busy", "young"]
    monkeypatch.setattr(settings, "scan_keep_minutes", 5)
    assert sorted(escl._scans(portal.app)) == ["busy"]


async def test_when_the_scans_take_more_than_the_megabytes_allowed_the_oldest_go_first(portal, monkeypatch):
    from spoolsteward import escl

    monkeypatch.setattr(settings, "scan_keep_mb", 1)
    mb = 1 << 20
    _held(portal.app, a=(600_000, 300), b=(600_000, 200), c=(600_000, 100))                     # 1.8 MB, the limit is 1 MB
    assert list(escl._scans(portal.app)) == ["c"]                                              # a and b (the oldest) are dropped
    _held(portal.app, a=(300_000, 300), b=(300_000, 200), c=(300_000, 100))                     # 0.9 MB: fits
    assert sorted(escl._scans(portal.app)) == ["a", "b", "c"]
    _held(portal.app, a=(500_000, 300), b=(500_000, 200), c=(500_000, 100))                     # 1.5 MB: only the oldest has to go
    assert sorted(escl._scans(portal.app)) == ["b", "c"]
    _held(portal.app, big=(3 * mb, 10))                                                         # alone and over the limit: kept, it is the newest
    assert list(escl._scans(portal.app)) == ["big"]
    d = _held(portal.app, old=(600_000, 300), new=(3 * mb, 10))
    d["running"] = {"user_id": 1, "state": "running", "name": "r", "ts": 0.0}                   # running scans and failed ones hold no data
    d["failed"] = {"user_id": 1, "state": "error", "name": "f", "ts": 0.0, "error": "x"}
    monkeypatch.setattr(settings, "scan_keep_minutes", 99999)
    assert sorted(escl._scans(portal.app)) == ["failed", "new", "running"]


async def test_a_scan_that_has_just_finished_pushes_out_the_oldest_at_once(portal, monkeypatch):
    from spoolsteward import escl

    monkeypatch.setattr(settings, "scan_keep_mb", 1)
    _held(portal.app, first=(700_000, 60))

    async def big(app, user, scanner, source, color, dpi, fmt):
        return b"y" * 700_000, "application/pdf", 1, "pdf"
    monkeypatch.setattr(escl, "scan_once", big)
    rec = {"user_id": 1, "state": "running", "name": "second", "ts": 0.0}
    portal.app.state.web_scans["second"] = rec
    await escl._run_scan(portal.app, rec, None, None, "platen", "color", 300, "pdf")
    assert rec["state"] == "done" and list(portal.app.state.web_scans) == ["second"]            # no page was opened: it happened by itself


async def test_with_delete_after_download_the_scan_is_gone_once_it_was_downloaded(scanner, monkeypatch):
    monkeypatch.setattr(settings, "scan_delete_after_download", True)
    scanner.app.state.web_scans = {}                    # the app is shared by the tests of this file
    await portal_login(scanner, "rita", GOOD)
    tok = await csrf_of_page(scanner, "/me/scan")
    st, r = await do_scan(scanner, tok, source="platen", color="color", dpi="300", fmt="pdf")
    assert st["state"] == "done" and r.status_code == 200 and r.content == b"%PDF-fake"
    assert r.headers["content-length"] == str(len(b"%PDF-fake")) and "attachment" in r.headers["content-disposition"]
    assert scanner.app.state.web_scans == {}                                                    # out of the memory
    page = (await scanner.get("/me/scan")).text                                                 # and out of the page
    assert f"/me/scan/{st['id']}" not in page                                                   # no link to it any more
    assert (await scanner.get(f"/me/scan/{st['id']}/file")).status_code == 404


async def test_without_it_the_scan_can_be_downloaded_again_until_its_time_is_over(scanner, monkeypatch):
    monkeypatch.setattr(settings, "scan_delete_after_download", False)
    scanner.app.state.web_scans = {}
    await portal_login(scanner, "rita", GOOD)
    tok = await csrf_of_page(scanner, "/me/scan")
    st, first = await do_scan(scanner, tok, source="platen", color="color", dpi="300", fmt="pdf")
    again = await scanner.get(f"/me/scan/{st['id']}/file")
    assert first.content == again.content == b"%PDF-fake" and len(scanner.app.state.web_scans) == 1


async def test_the_page_says_how_many_minutes_the_scan_stays(scanner, monkeypatch):
    await portal_login(scanner, "rita", GOOD)
    for minutes, said in ((1, "for 1 minute."), (10, "for 10 minutes.")):
        monkeypatch.setattr(settings, "scan_keep_minutes", minutes)
        assert said in (await scanner.get("/me/scan")).text


def test_the_waiting_note_is_translated_with_the_right_form_of_minute():
    from spoolsteward import i18n

    base = "Scanning… with several sheets it can take a minute. You can also leave this page: the file stays below for %(n)s "
    ask = lambda n: i18n.ngettext(base + "minute.", base + "minutes.", n, n=n)       # noqa: E731
    expected = {"it": {1: "per 1 minuto.", 10: "per 10 minuti."}, "es": {1: "durante 1 minuto.", 10: "durante 10 minutos."},
                "ru": {1: "на 1 минуту.", 2: "на 2 минуты.", 5: "на 5 минут.", 21: "на 21 минуту."}, "zh": {1: "保留 1 分钟。", 10: "保留 10 分钟。"}}
    for lang, cases in expected.items():
        with i18n.using(lang):
            for n, tail in cases.items():
                assert str(ask(n)).endswith(tail), (lang, n, str(ask(n)))
