"""Periodic maintenance print and test page (physical printers only, one shared setting per queue), and the order
printers are listed in (physical first, oldest connection first). A fake CUPS answers over IPP."""

import io
import re
from datetime import UTC, datetime, timedelta

import httpx
import pypdf
import pytest
from sqlalchemy import select

from spoolsteward.cups_client import list_queues
from spoolsteward.db.models import AdminAudit, PrinterKeepalive, PrinterSeen
from spoolsteward.ipp import codec
from spoolsteward.policy import keepalive, ordering, testpage
from tests.conftest import csrf_of_page, make_user, portal_login

GOOD = "correct horse battery"


class FakeCups:
    """CUPS-Get-Printers lists `queues` [(name, device_uri, color)]; Print-Job is recorded; admin ops succeed."""

    def __init__(self, queues):
        self.queues, self.jobs = list(queues), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        msg = codec.decode(request.content)
        ok = codec.IppMessage((2, 0), 0, 1, [codec.Group(codec.TAG_OPERATION, [
            codec.Attr("attributes-charset", [codec.Val(codec.TAG_CHARSET, "utf-8")]),
            codec.Attr("attributes-natural-language", [codec.Val(codec.TAG_NATURAL_LANG, "en")])])])
        if msg.code == 0x4002:
            for name, uri, color in self.queues:
                ok.groups.append(codec.Group(codec.TAG_PRINTER, [
                    codec.Attr("printer-name", [codec.Val(codec.TAG_NAME, name)]),
                    codec.Attr("printer-info", [codec.Val(codec.TAG_TEXT, f"Info {name}")]),
                    codec.Attr("device-uri", [codec.Val(codec.TAG_URI, uri)]),
                    codec.Attr("printer-state", [codec.Val(codec.TAG_ENUM, 3)]),
                    codec.Attr("color-supported", [codec.Val(codec.TAG_BOOLEAN, color)]),
                    codec.Attr("marker-names", [codec.Val(codec.TAG_NAME, "Black")]),
                    codec.Attr("marker-levels", [codec.Val(codec.TAG_INTEGER, 80)]),
                    codec.Attr("marker-colors", [codec.Val(codec.TAG_NAME, "#000000")])]))
        elif msg.code == codec.PRINT_JOB:
            job = msg.group(codec.TAG_JOB)
            self.jobs.append({"queue": request.url.path.rsplit("/", 1)[-1], "data": msg.data,
                              "color": job.get("print-color-mode").first if job else None,
                              "user": msg.get(codec.TAG_OPERATION, "requesting-user-name")})
        return httpx.Response(200, content=codec.encode(ok), headers={"content-type": "application/ipp"})


PHYS = "ipp://192.0.2.10:631/ipp/print"


@pytest.fixture
async def cups(portal, monkeypatch):
    import spoolsteward.admin.printers as printers_mod
    import spoolsteward.cups_client as cc

    async def up(uri, usb_present=None):
        return True
    monkeypatch.setattr(cc, "is_online", up)
    monkeypatch.setattr(printers_mod, "is_online", up)

    async def no_device(base):
        return {}
    monkeypatch.setattr(printers_mod, "_device", no_device)  # no network: the printer's own web service is not asked
    fake = FakeCups([("virtual", "cups-pdf:/", True), ("mono1", PHYS, False), ("col1", "ipp://192.0.2.11:631/ipp/print", True)])
    portal.fake = fake
    portal.app.state.upstream = httpx.AsyncClient(transport=httpx.MockTransport(fake), base_url="http://localhost")
    portal.app.state.queues_cache = portal.app.state.printers_cache = None
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    make_user(portal.sm, "boss2", password=GOOD, admin=True)
    make_user(portal.sm, "ann", password=GOOD, group="staff")
    await portal_login(portal, "boss", GOOD)
    yield portal
    await portal.app.state.upstream.aclose()


# ---------------------------------------------------------------- the PDF

def test_the_maintenance_and_test_pages_are_valid_pdfs():
    pdf = testpage.build("Title (with) parentheses \\ and àè", "sub", ink=[{"name": "Black", "level": 40, "color": "#000000"}, {"name": "?", "level": -1}],
                         sections=[("S", [("a", "b"), ("empty", "")])], swatches="color", stripes=True)
    r = pypdf.PdfReader(io.BytesIO(pdf))
    assert len(r.pages) >= 1 and "Title (with) parentheses" in r.pages[0].extract_text() and "40%" in r.pages[0].extract_text()
    long = testpage.build("x", sections=[("S", [(f"row{i}", "v") for i in range(200)])])
    assert len(pypdf.PdfReader(io.BytesIO(long)).pages) > 1
    assert pypdf.PdfReader(io.BytesIO(keepalive.maintenance_page("q", "Printer", False, 168))).pages


# ---------------------------------------------------------------- interval rules

def test_interval_is_validated():
    assert keepalive.parse_interval("7", "days") == 168 and keepalive.parse_interval("12", "hours") == 12
    for amount, unit in (("0", "days"), ("91", "days"), ("-1", "days"), ("1.5", "days"), ("", "days"), ("3", "weeks"), ("٣", "days")):
        with pytest.raises(ValueError):
            keepalive.parse_interval(amount, unit)
    assert keepalive.describe(24) == "every day" and keepalive.describe(168) == "every 7 days" and keepalive.describe(5) == "every 5 hours"
    assert keepalive.split(168) == (7, "days") and keepalive.split(5) == (5, "hours")


def test_next_due_counts_from_the_last_success_not_from_attempts():
    now = datetime.now(UTC)
    r = PrinterKeepalive(printer="p", enabled=True, interval_hours=24, enabled_at=now - timedelta(hours=30))
    assert keepalive.next_due(r) == now - timedelta(hours=6)
    r.last_ok_at, r.last_attempt_at, r.last_status = now - timedelta(hours=2), now - timedelta(minutes=10), "error"
    assert keepalive.next_due(r) == now - timedelta(hours=2) + timedelta(hours=24)                     # a failure does not reset the clock...
    r.last_ok_at = now - timedelta(hours=30)
    assert keepalive.next_due(r) == now - timedelta(minutes=10) + keepalive.RETRY_AFTER                # ...but waits before retrying
    r.enabled = False
    assert keepalive.next_due(r) is None


# ---------------------------------------------------------------- order

async def test_physical_printers_come_first_oldest_connection_first(cups):
    order = lambda qs: [q["name"] for q in qs]  # noqa: E731
    assert order(await list_queues(cups.app)) == ["col1", "mono1", "virtual"]       # first sighting: all at once, alphabetical
    cups.fake.queues.insert(0, ("aaa-new", "ipp://192.0.2.12:631/ipp/print", False))  # connected later, sorts first by name
    cups.fake.queues.append(("debugprinter0", "cups-pdf:/", True))
    cups.app.state.queues_cache = None
    assert order(await list_queues(cups.app)) == ["col1", "mono1", "aaa-new", "virtual", "debugprinter0"]
    page = (await cups.get("/admin/printers")).text
    names = re.findall(r'<b class="mono">([^<]+)</b>\s*\n', page)
    assert names == ["col1", "mono1", "aaa-new", "virtual", "debugprinter0"]
    assert [m for m in re.findall(r'value="([^"]+)"\s+(?:checked\s+)?required', (await cups.get("/me/print")).text)] == names


def test_a_deleted_queue_loses_its_place(portal):
    ordering.note_seen(portal.sm, ["Old"])
    with portal.sm() as s:
        ordering.forget(s, "OLD")
        s.commit()
        assert s.scalars(select(PrinterSeen)).first() is None


# ---------------------------------------------------------------- buttons

async def test_buttons_exist_for_physical_printers_only(cups):
    page = (await cups.get("/admin/printers")).text
    for name in ("mono1", "col1"):
        assert f"/admin/printers/{name}/keepalive" in page and f"/admin/printers/{name}/testprint" in page
    assert "/admin/printers/virtual/keepalive" not in page and "/admin/printers/virtual/testprint" not in page
    tok = await csrf_of_page(cups, "/admin/printers")
    for path in ("keepalive/toggle", "keepalive", "testprint"):                  # and the server refuses them too
        r = await cups.post(f"/admin/printers/virtual/{path}", data={"csrf_token": tok, "on": "1", "mode": "mono", "amount": "7", "unit": "days"})
        assert r.status_code == 303
    assert "is a virtual printer" in (await cups.get("/admin/printers")).text
    assert cups.fake.jobs == []
    with cups.sm() as s:
        assert s.scalars(select(PrinterKeepalive)).first() is None
    r = await cups.post("/admin/printers/nope/testprint", data={"csrf_token": tok, "mode": "mono"})
    assert r.status_code == 303 and cups.fake.jobs == []


async def test_save_does_not_switch_on_and_the_toggle_is_shared_by_all_admins(cups):
    tok = await csrf_of_page(cups, "/admin/printers")
    r = await cups.post("/admin/printers/mono1/keepalive", data={"csrf_token": tok, "amount": "3", "unit": "days"})
    assert r.status_code == 303
    with cups.sm() as s:
        row = s.get(PrinterKeepalive, "mono1")
        assert (row.enabled, row.interval_hours, row.color) == (False, 72, False)
    await cups.post("/admin/printers/mono1/keepalive/toggle", data={"csrf_token": tok, "on": "1"})
    page = (await cups.get("/admin/printers")).text
    assert "Periodic print active: every 3 days" in page
    await cups.post("/logout", data={"csrf_token": tok})                          # another administrator sees the same state
    await portal_login(cups, "boss2", GOOD)
    assert "Periodic print active: every 3 days" in (await cups.get("/admin/printers")).text
    tok = await csrf_of_page(cups, "/admin/printers")
    await cups.post("/admin/printers/mono1/keepalive", data={"csrf_token": tok, "amount": "2", "unit": "days"})
    with cups.sm() as s:
        row = s.get(PrinterKeepalive, "mono1")
        assert row.enabled and row.interval_hours == 48 and row.updated_by == "boss2"   # saving left it on
        assert {a.action for a in s.scalars(select(AdminAudit))} >= {"printer.keepalive_on", "printer.keepalive_save"}


async def test_bad_input_and_colour_on_a_mono_printer_are_refused(cups):
    tok = await csrf_of_page(cups, "/admin/printers")
    for data in ({"amount": "0", "unit": "days"}, {"amount": "500", "unit": "days"}, {"amount": "x", "unit": "days"},
                 {"amount": "7", "unit": "days", "color": "1"}):                       # mono1 has no colour
        await cups.post("/admin/printers/mono1/keepalive", data={"csrf_token": tok, **data})
    with cups.sm() as s:
        assert s.get(PrinterKeepalive, "mono1") is None
    await cups.post("/admin/printers/col1/keepalive", data={"csrf_token": tok, "amount": "7", "unit": "days", "color": "1"})
    with cups.sm() as s:
        assert s.get(PrinterKeepalive, "col1").color is True
    assert (await cups.post("/admin/printers/col1/keepalive", data={"amount": "7", "unit": "days"})).status_code == 403   # CSRF


async def test_only_admins_can_use_them(cups):
    await cups.post("/logout", data={"csrf_token": await csrf_of_page(cups, "/admin/printers")})
    await portal_login(cups, "ann", GOOD)
    tok = await csrf_of_page(cups, "/me")
    for path in ("keepalive/toggle", "keepalive", "testprint"):
        assert (await cups.post(f"/admin/printers/col1/{path}", data={"csrf_token": tok, "on": "1", "mode": "mono"})).status_code in (302, 303, 403)
    assert cups.fake.jobs == []
    with cups.sm() as s:
        assert s.scalars(select(PrinterKeepalive)).first() is None


# ---------------------------------------------------------------- test print

async def test_test_print_sends_the_statistics_in_the_chosen_mode(cups):
    tok = await csrf_of_page(cups, "/admin/printers")
    await cups.post("/admin/printers/col1/testprint", data={"csrf_token": tok, "mode": "color"})
    await cups.post("/admin/printers/mono1/testprint", data={"csrf_token": tok, "mode": "mono"})
    await cups.post("/admin/printers/mono1/testprint", data={"csrf_token": tok, "mode": "color"})   # not offered: refused
    assert [(j["queue"], j["color"]) for j in cups.fake.jobs] == [("col1", "color"), ("mono1", "monochrome")]
    assert all(j["user"].endswith("maintenance") for j in cups.fake.jobs)
    text = pypdf.PdfReader(io.BytesIO(cups.fake.jobs[0]["data"])).pages[0].extract_text()
    assert "Test print" in text and "Black" in text and "80%" in text and "From the gateway" in text
    with cups.sm() as s:
        assert [a.action for a in s.scalars(select(AdminAudit)) if a.action == "printer.test_print"] == ["printer.test_print"] * 2


# ---------------------------------------------------------------- scheduler

async def test_scheduler_prints_only_due_enabled_reachable_physical_queues(cups, monkeypatch):
    now = datetime.now(UTC)
    with cups.sm() as s:
        s.add_all([
            PrinterKeepalive(printer="col1", enabled=True, interval_hours=24, color=True, enabled_at=now - timedelta(hours=25)),     # due
            PrinterKeepalive(printer="mono1", enabled=True, interval_hours=24, color=True, enabled_at=now - timedelta(hours=1)),    # not yet
            PrinterKeepalive(printer="virtual", enabled=True, interval_hours=1, enabled_at=now - timedelta(days=3)),                # virtual
            PrinterKeepalive(printer="gone", enabled=True, interval_hours=1, enabled_at=now - timedelta(days=3)),                   # no queue
        ])
        s.commit()
    assert await keepalive.run_due(cups.app) == 1
    assert [(j["queue"], j["color"]) for j in cups.fake.jobs] == [("col1", "color")]
    with cups.sm() as s:
        assert s.get(PrinterKeepalive, "col1").last_status == "ok" and s.get(PrinterKeepalive, "col1").last_ok_at is not None
    assert await keepalive.run_due(cups.app) == 0                                       # not again until the next interval
    with cups.sm() as s:                                                                # a colour setting on a printer that lost colour: mono
        s.get(PrinterKeepalive, "mono1").enabled_at = now - timedelta(days=2)
        s.commit()
    assert await keepalive.run_due(cups.app) == 1 and cups.fake.jobs[-1]["color"] == "monochrome"


async def test_offline_printer_is_skipped_and_retried_when_back(cups, monkeypatch):
    import spoolsteward.cups_client as cc
    now = datetime.now(UTC)
    with cups.sm() as s:
        s.add(PrinterKeepalive(printer="col1", enabled=True, interval_hours=24, enabled_at=now - timedelta(days=2)))
        s.commit()

    async def down(uri, usb_present=None):
        return False
    monkeypatch.setattr(cc, "is_online", down)
    assert await keepalive.run_due(cups.app) == 0 and cups.fake.jobs == []
    with cups.sm() as s:
        assert s.get(PrinterKeepalive, "col1").last_status == "offline" and s.get(PrinterKeepalive, "col1").last_attempt_at is None
    assert "I will retry as soon as it answers" in (await cups.get("/admin/printers")).text
    async def up(uri, usb_present=None):
        return True
    monkeypatch.setattr(cc, "is_online", up)
    cups.app.state.queues_cache = None
    assert await keepalive.run_due(cups.app) == 1


async def test_failed_print_is_recorded_and_retried_later(cups):
    now = datetime.now(UTC)
    with cups.sm() as s:
        s.add(PrinterKeepalive(printer="col1", enabled=True, interval_hours=24, enabled_at=now - timedelta(days=2)))
        s.commit()
    await cups.app.state.upstream.aclose()
    cups.app.state.upstream = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=codec.encode(codec.make_error_response(0x0500, 1)) if codec.decode(r.content).code == codec.PRINT_JOB
                                 else cups.fake(r).content, headers={"content-type": "application/ipp"})), base_url="http://localhost")
    assert await keepalive.run_due(cups.app) == 0
    with cups.sm() as s:
        row = s.get(PrinterKeepalive, "col1")
        assert row.last_status == "error" and "refused" in row.last_error and row.last_ok_at is None
    assert await keepalive.run_due(cups.app) == 0                                       # waits RETRY_AFTER before trying again
    assert "last attempt failed" in (await cups.get("/admin/printers")).text


# ---------------------------------------------------------------- languages

@pytest.mark.parametrize("code", ["en", "it", "es", "ru", "zh"])
async def test_printers_page_and_pages_it_prints_work_in_every_language(cups, code):
    r = await cups.get("/admin/printers", headers={"Accept-Language": code})
    assert r.status_code == 200 and f'<html lang="{code}"' in r.text and "%(" not in r.text
    tok = await csrf_of_page(cups, "/admin/printers")
    cups.fake.jobs.clear()
    await cups.post("/admin/printers/col1/testprint", data={"csrf_token": tok, "mode": "color"}, headers={"Accept-Language": code})
    # the pages the gateway prints are English whatever the admin's language (no font for Russian / Chinese yet)
    assert "Test print" in pypdf.PdfReader(io.BytesIO(cups.fake.jobs[0]["data"])).pages[0].extract_text()


async def test_counter_labels_and_test_printer_names_follow_the_language(cups, monkeypatch):
    import spoolsteward.admin.printers as printers_mod
    from spoolsteward.i18n import _catalog

    async def device(base):
        return {"ipp": {}, "usage": [(label, 1) for label in printers_mod.LEDM_USAGE.values()], "config": {}, "scanner": None}
    monkeypatch.setattr(printers_mod, "_device", device)
    cups.fake.queues.append(("debugprinter0", "cups-pdf:/", True))     # CUPS stored its description in some language
    cups.app.state.queues_cache = cups.app.state.printers_cache = None
    for label in printers_mod.LEDM_USAGE.values():                       # every label is a catalogued text, none a literal
        for code in ("it", "es", "ru", "zh"):
            assert _catalog(code).gettext(label) != label, (code, label)
    page = (await cups.get("/admin/printers", headers={"Accept-Language": "ru"})).text
    assert "Замятия" in page and "Jams" not in page
    assert "Тестовый принтер (debugprinter0)" in (await cups.get("/me/print", headers={"Accept-Language": "ru"})).text


# ---------------------------------------------------------------- automatic refresh switch

def test_the_printers_refresh_interval_is_configurable_and_out_of_range_is_an_error(monkeypatch):
    from pydantic import ValidationError

    from spoolsteward.config import Settings
    for raw, want in (("5", 5), ("30", 30), ("3600", 3600), ("15", 15)):
        monkeypatch.setenv("SPOOLSTEWARD_PRINTERS_REFRESH_SECONDS", raw)
        assert Settings().printers_refresh_seconds == want, raw
    for raw in ("0", "-3", "1", "4", "3601", "99999", "", "soon"):      # no silent clamping any more
        monkeypatch.setenv("SPOOLSTEWARD_PRINTERS_REFRESH_SECONDS", raw)
        with pytest.raises(ValidationError):
            Settings()
    monkeypatch.delenv("SPOOLSTEWARD_PRINTERS_REFRESH_SECONDS")
    assert Settings().printers_refresh_seconds == 15


async def test_printers_page_has_the_refresh_switch_and_uses_the_configured_interval(cups, monkeypatch):
    from spoolsteward.config import settings
    page = (await cups.get("/admin/printers")).text
    assert 'id="ar-printers"' in page and 'data-ar-secs="15"' in page and 'data-ar-on="0"' in page      # off at first, 15 s by default
    monkeypatch.setattr(settings, "printers_refresh_seconds", 40)
    page = (await cups.get("/admin/printers")).text
    assert 'data-ar-secs="40"' in page


async def test_the_cache_follows_the_configured_interval(cups, monkeypatch):
    from spoolsteward.config import settings
    monkeypatch.setattr(settings, "printers_refresh_seconds", 5)
    await cups.get("/admin/printers")
    stamp = cups.app.state.printers_cache[0]
    await cups.get("/admin/printers")
    assert cups.app.state.printers_cache[0] == stamp                                           # still fresh
    cups.app.state.printers_cache = (stamp - 6, cups.app.state.printers_cache[1])
    await cups.get("/admin/printers")
    assert cups.app.state.printers_cache[0] > stamp - 6                                        # older than 5 s: read again


async def test_audit_page_has_the_switch_too(cups):
    page = (await cups.get("/admin/jobs")).text
    assert 'id="ar-audit"' in page and 'data-ar-on="1"' in page and 'data-ar-secs="30"' in page           # the user's setting (30 s) starts it on
    assert (await cups.post("/me/settings/refresh", data={"csrf_token": await csrf_of_page(cups, "/me/settings"), "seconds": "0"})).status_code == 303
    page = (await cups.get("/admin/jobs")).text
    assert 'data-ar-on="0"' in page and 'data-ar-secs="30"' in page                                       # 0 = off at first, 30 s when switched on
