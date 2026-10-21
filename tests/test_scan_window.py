"""The scan window: a button on /me/scan that lets the scan app at the same address scan without credentials."""

import httpx
import pytest
from sqlalchemy import select

from app import notices
from pydantic import ValidationError

from app.config import Settings, settings
from app.db.models import JobLog, JobState, Notice
from app.escl_window import Windows, normalize_ip
from tests.conftest import csrf_of_page, portal_login
from tests.test_escl import GOOD, scan_rows, scanner  # noqa: F401  (the fixture)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(scanner, monkeypatch):  # noqa: F811
    c = Clock()
    scanner.app.state.scan_windows = Windows(clock=c)
    monkeypatch.setattr(settings, "escl_window", True)
    monkeypatch.setattr(settings, "escl_anonymous", False)
    monkeypatch.setattr(settings, "escl_window_seconds", 180)
    notices._state.clear()
    yield c
    scanner.app.state.scan_windows = None
    scanner.app.state.__dict__.pop("scan_jobs", None)       # the app object outlives the test


async def press(cl):
    """Log in as rita and press the button from the client's own address (10.0.0.7)."""
    await portal_login(cl, "rita", GOOD)
    tok = await csrf_of_page(cl, "/me/scan")
    r = await cl.post("/me/scan/window", data={"csrf_token": tok})
    assert r.status_code == 200, r.text
    return tok, r.json()


def elsewhere(cl, ip):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=cl.app, client=(ip, 5000)), base_url="https://gw.test")


def notices_of(cl, kind):
    with cl.sm() as s:
        return list(s.scalars(select(Notice).where(Notice.kind == kind)))


# ---------------------------------------------------------------- settings

def test_seconds_setting_bounds():
    assert Settings().escl_window_seconds == 180 and Settings().escl_window is True
    assert Settings(escl_window_seconds="60").escl_window_seconds == 60 and Settings(escl_window_seconds="900").escl_window_seconds == 900
    for bad in ("59", "901", "0", "-5", "abc", "", "none", "1e2"):        # nothing is corrected silently, not even an empty line
        with pytest.raises(ValidationError):
            Settings(escl_window_seconds=bad)


def test_the_start_is_refused_with_a_bad_value():
    """The real start (python -m app), not a function: wrong value in the environment -> exit 2 and the list on stderr."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {**os.environ, "SPOOLSTEWARD_ESCL_WINDOW_SECONDS": "30", "SPOOLSTEWARD_ALLOW_INSECURE_AUTH": "true"}
    r = subprocess.run([sys.executable, "-m", "app"], cwd=Path(__file__).resolve().parent.parent, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    assert "refusing to start: wrong settings" in r.stderr and "SPOOLSTEWARD_ESCL_WINDOW_SECONDS" in r.stderr
    assert "Traceback" not in r.stderr


def test_addresses_are_compared_in_one_spelling():
    assert normalize_ip("::ffff:192.168.1.5") == "192.168.1.5"
    assert normalize_ip("fe80::1%eth0") == "fe80::1"
    assert normalize_ip("2001:DB8::1") == "2001:db8::1"
    assert normalize_ip("not an ip") == "" and normalize_ip("") == "" and normalize_ip(None) == ""


def test_registry_basics():
    c = Clock()
    w = Windows(clock=c)
    w.open("10.0.0.1", 1, "a", 60)
    w.open("10.0.0.1", 2, "b", 60)              # the same address: the last one pressed is the one that is expected
    assert w.for_ip("10.0.0.1").user_id == 2 and w.of_user(1) is None
    w.open("10.0.0.2", 2, "b", 60)              # one window per person: it moved
    assert w.for_ip("10.0.0.1") is None and w.for_ip("10.0.0.2").user_id == 2
    c.t += 61
    assert w.for_ip("10.0.0.2") is None and w.remaining(2) == 0
    assert w.mismatch_due("a", "b") and not w.mismatch_due("a", "b")
    c.t += 61
    assert w.mismatch_due("a", "b")


# ---------------------------------------------------------------- the page

async def test_button_is_absent_when_the_feature_is_off_and_present_when_on(scanner, clock, monkeypatch):  # noqa: F811
    await portal_login(scanner, "rita", GOOD)
    assert 'id="winbtn"' in (await scanner.get("/me/scan")).text
    monkeypatch.setattr(settings, "escl_window", False)
    page = (await scanner.get("/me/scan")).text
    assert "winbtn" not in page and "windlg" not in page and "scan/window" not in page
    tok = await csrf_of_page(scanner, "/me/scan")
    assert (await scanner.post("/me/scan/window", data={"csrf_token": tok})).status_code == 404


async def test_button_also_shows_with_anonymous_scans_on(scanner, clock, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "escl_anonymous", True)
    await portal_login(scanner, "rita", GOOD)
    assert 'id="winbtn"' in (await scanner.get("/me/scan")).text


async def test_pressing_needs_the_csrf_token_and_tells_the_address(scanner, clock):  # noqa: F811
    await portal_login(scanner, "rita", GOOD)
    assert (await scanner.post("/me/scan/window", data={"csrf_token": "x"})).status_code == 403
    tok, info = await press(scanner)
    assert info["ip"] == "10.0.0.7" and info["seconds"] == 180 and info["ipv6"] is False and info["proxy"] is False
    r = await scanner.post("/me/scan/window", data={"csrf_token": tok}, headers={"X-Forwarded-For": "1.2.3.4"})
    assert r.json()["proxy"] is True


# ---------------------------------------------------------------- the gate

async def test_scan_from_the_window_address_is_the_persons_own(scanner, clock):  # noqa: F811
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401      # nothing open yet
    await press(scanner)
    assert (await scanner.get("/eSCL/ScannerCapabilities")).status_code == 200
    r = await scanner.post("/eSCL/ScanJobs", content=b"<x/>")
    assert r.status_code == 201
    assert (await scanner.get("/eSCL/ScanJobs/j1/NextDocument")).content == b"%PDF-fake"
    assert [(k.value, s.value, u) for k, s, _p, u in scan_rows(scanner)][-1] == ("scan", "completed", "rita")
    with scanner.sm() as s:
        row = s.scalars(select(JobLog).where(JobLog.state == JobState.completed)).one()
        assert row.options_json["window_ip"] == "10.0.0.7" and "guest_ip" not in row.options_json


async def test_another_address_is_refused_and_both_addresses_are_reported(scanner, clock):  # noqa: F811
    await press(scanner)
    async with elsewhere(scanner, "10.0.0.99") as other:
        for _ in range(5):                                                       # a program that keeps trying
            assert (await other.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401
    with scanner.sm() as s:
        denied = list(s.scalars(select(JobLog).where(JobLog.state == JobState.denied)))
    assert len(denied) == 1 and denied[0].user_name == "rita"                    # once per pair, not once per attempt
    assert denied[0].options_json["window_ip"] == "10.0.0.7" and denied[0].options_json["got_ip"] == "10.0.0.99"
    assert "10.0.0.7" in denied[0].denied_reason and "10.0.0.99" in denied[0].denied_reason
    (n,) = notices_of(scanner, "scan_window_mismatch")
    assert n.detail_json == {"expected": "10.0.0.7", "got": "10.0.0.99"} and n.user_id == denied[0].user_id
    await portal_login(scanner, "rita", GOOD)
    page = (await scanner.get("/me/notices")).text
    assert "10.0.0.7" in page and "10.0.0.99" in page
    assert scan_rows(scanner).count((scan_rows(scanner)[0][0], JobState.completed, 1, "rita")) == 0   # nothing was scanned


async def test_nobody_is_told_when_no_window_is_open(scanner, clock):  # noqa: F811
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401
    assert notices_of(scanner, "scan_window_mismatch") == []
    with scanner.sm() as s:
        assert s.scalars(select(JobLog)).first() is None


async def test_credentials_still_work_and_wrong_ones_still_fail(scanner, clock):  # noqa: F811
    await press(scanner)
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>", auth=("rita", "wrong"))).status_code == 401
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>", auth=("sam", scanner.key2))).status_code == 201
    assert scan_rows(scanner)[-1][3] == "sam"


async def test_window_ending_during_a_scan_does_not_cut_it_off(scanner, clock):  # noqa: F811
    await press(scanner)
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 201
    clock.t += 3600                                                              # the window is long over
    assert (await scanner.get("/eSCL/ScanJobs/j1/NextDocument")).content == b"%PDF-fake"   # the page still arrives
    assert (await scanner.delete("/eSCL/ScanJobs/j1")).status_code == 200      # and the app can close the job
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401      # but no new scan may start
    assert scan_rows(scanner)[0][1] is JobState.completed


async def test_a_job_can_be_followed_only_for_a_limited_time(scanner, clock, monkeypatch):  # noqa: F811
    from app import escl

    await press(scanner)
    await scanner.post("/eSCL/ScanJobs", content=b"<x/>")
    real = escl.time.monotonic
    monkeypatch.setattr(escl.time, "monotonic", lambda: real() + escl.JOB_FOLLOW_SECONDS + 1)
    clock.t += 3600                                                              # and the window is over too
    assert (await scanner.get("/eSCL/ScanJobs/j1/NextDocument")).status_code == 401


async def test_a_finished_job_is_not_a_back_door(scanner, clock):  # noqa: F811
    await press(scanner)
    await scanner.post("/eSCL/ScanJobs", content=b"<x/>")
    await scanner.delete("/eSCL/ScanJobs/j1")
    clock.t += 3600
    assert (await scanner.get("/eSCL/ScanJobs/j1/NextDocument")).status_code == 401
    async with elsewhere(scanner, "10.0.0.99") as other:                         # another address never inherits the job
        assert (await other.get("/eSCL/ScanJobs/j1/NextDocument")).status_code == 401


async def test_closing_the_window_stops_new_scans_but_not_the_running_one(scanner, clock):  # noqa: F811
    tok, _ = await press(scanner)
    await scanner.post("/eSCL/ScanJobs", content=b"<x/>")
    assert (await scanner.post("/me/scan/window/close", data={"csrf_token": tok})).json() == {"closed": True}
    assert (await scanner.get("/eSCL/ScanJobs/j1/NextDocument")).content == b"%PDF-fake"
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401


async def test_a_new_press_renews_and_ipv4_mapped_addresses_match(scanner, clock):  # noqa: F811
    tok, _ = await press(scanner)
    clock.t += 170
    await scanner.post("/me/scan/window", data={"csrf_token": tok})
    clock.t += 170                                                               # past the first end, inside the renewed one
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 201
    async with elsewhere(scanner, "::ffff:10.0.0.7") as mapped:                  # the same address, spelled as IPv6
        assert (await mapped.get("/eSCL/ScanJobs/j1/NextDocument")).status_code == 200


async def test_with_anonymous_on_the_window_gives_the_scan_to_the_person(scanner, clock, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "escl_anonymous", True)
    async with elsewhere(scanner, "10.0.0.50") as stranger:
        assert (await stranger.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 201   # no window: the guest
    assert scan_rows(scanner)[-1][3] == "unknown-guest"
    await press(scanner)
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 201
    assert scan_rows(scanner)[-1][3] == "rita"


async def test_a_disabled_account_loses_its_window(scanner, clock):  # noqa: F811
    await press(scanner)
    with scanner.sm() as s:
        from app.db.models import User
        s.scalars(select(User).where(User.username == "rita")).one().enabled = False
        s.commit()
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401


async def test_feature_off_ignores_open_windows(scanner, clock, monkeypatch):  # noqa: F811
    await press(scanner)
    monkeypatch.setattr(settings, "escl_window", False)
    assert (await scanner.post("/eSCL/ScanJobs", content=b"<x/>")).status_code == 401
    assert notices_of(scanner, "scan_window_mismatch") == []


async def test_too_many_windows_are_refused(scanner, clock, monkeypatch):  # noqa: F811
    from app import escl_window

    monkeypatch.setattr(escl_window, "MAX_WINDOWS", 0)
    await portal_login(scanner, "rita", GOOD)
    tok = await csrf_of_page(scanner, "/me/scan")
    assert (await scanner.post("/me/scan/window", data={"csrf_token": tok})).status_code == 503


def test_the_popup_link_points_at_a_real_section_of_the_user_guide():
    from pathlib import Path

    from app.admin.docs import USER_PAGES, slugify
    from app.escl import WINDOW_DOC

    root = Path(__file__).resolve().parent.parent / "docs"
    for lang, (page, heading) in WINDOW_DOC.items():
        assert page in USER_PAGES[lang]                       # users who are not administrators can open that page
        text = (root / lang / f"{page}.md").read_text(encoding="utf-8")
        assert f"### {heading}\n" in text and slugify(heading)


async def test_the_popup_link_is_in_the_page(scanner, clock):  # noqa: F811
    await portal_login(scanner, "rita", GOOD)
    page = (await scanner.get("/me/scan")).text
    assert 'href="/docs/it/guida-utente#scansione-da-unapp-che-non-sa-accedere"' in page
