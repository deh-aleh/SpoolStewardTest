"""Pins POST /admin/printers/{name}/rename branch by branch (messages, redirects, what is sent to CUPS and in which order,
audit row, notices), so that the function can be split into helpers without changing anything."""

import html
import re

import httpx
import pytest
from sqlalchemy import select

from app.db.models import AdminAudit, Notice
from tests.conftest import csrf_of_page
from tests.test_discovery import usb  # noqa: F401  (the fixture: an administrator and a fake CUPS with the queues hp and lan)

pytestmark = pytest.mark.usefixtures("english")


@pytest.fixture
def cl(usb):  # noqa: F811
    return usb


async def rename(cl, name, new, **extra):
    """(status, location, flashes shown on the next page)"""
    tok = await csrf_of_page(cl, "/admin/printers")
    r = await cl.post(f"/admin/printers/{name}/rename", data={"csrf_token": tok, "new_name": new, **extra})
    if r.status_code != 303:
        return r.status_code, None, []
    page = (await cl.get(r.headers["location"])).text
    return r.status_code, r.headers["location"], [html.unescape(m) for m in re.findall(r'class="flash (?:ok|err)">([^<]*)<', page)]


def rename_audits(cl):
    with cl.sm() as s:
        return [(a.action, a.target_type, a.target_id, a.detail_json) for a in s.scalars(select(AdminAudit).where(AdminAudit.action == "printer.rename"))]


def broadcast(cl):
    with cl.sm() as s:
        return [(n.kind, n.detail_json) for n in s.scalars(select(Notice).where(Notice.kind == "printer_renamed"))]


async def test_a_name_that_is_not_a_queue_name_is_a_404(cl):
    tok = await csrf_of_page(cl, "/admin/printers")
    r = await cl.post("/admin/printers/bad name!/rename", data={"csrf_token": tok, "new_name": "x"})
    assert r.status_code == 404 and cl.fake.added == []


@pytest.mark.parametrize("new", ["bad name!", "   ", "a" * 128])
async def test_a_bad_new_name(cl, new):
    assert await rename(cl, "lan", new) == (303, "/admin/printers", ["Queue names use letters, digits and . _ @ + - only (max 127)"])
    assert cl.fake.added == [] and cl.fake.deleted == []


async def test_the_new_name_is_stripped(cl):
    assert (await rename(cl, "lan", "  Office_1  "))[0] == 303
    assert cl.fake.added[-1][0] == "Office_1"


async def test_cups_does_not_answer(cl):
    def down(request):
        raise httpx.ConnectError("down")
    cl.app.state.upstream = httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://localhost")
    assert await rename(cl, "lan", "x1") == (303, "/admin/printers", ["CUPS does not answer"])
    assert rename_audits(cl) == []


async def test_unknown_printer_same_name_taken_name_jobs_waiting_no_driver(cl):
    assert await rename(cl, "nope", "x1") == (303, "/admin/printers", ["nope: printer not found"])
    assert await rename(cl, "lan", "LAN") == (303, "/admin/printers", ["That is already its name"])
    assert await rename(cl, "lan", "HP") == (303, "/admin/printers", ["A queue called HP already exists"])
    cl.fake.jobs = {"lan": 2}
    assert await rename(cl, "lan", "x1") == (303, "/admin/printers", ["lan has jobs waiting: rename it when the queue is empty"])
    cl.fake.jobs = {}
    cl.fake.queues.append(("raw", "socket://192.0.2.30:9100"))
    assert await rename(cl, "raw", "x2") == (303, "/admin/printers", ["raw can not be renamed: its driver is not known to the gateway"])
    assert cl.fake.added == [] and cl.fake.deleted == [] and rename_audits(cl) == []


async def test_the_checks_run_in_order_same_name_before_taken_before_jobs(cl):
    cl.fake.jobs = {"lan": 2}
    assert (await rename(cl, "lan", "lan"))[2] == ["That is already its name"]
    assert (await rename(cl, "lan", "hp"))[2] == ["A queue called hp already exists"]
    assert (await rename(cl, "nope", "bad name!"))[2] == ["Queue names use letters, digits and . _ @ + - only (max 127)"]


async def test_cups_refuses_the_new_queue(cl):
    cl.fake.refuse_add = True
    assert await rename(cl, "lan", "x1") == (303, "/admin/printers", ["CUPS did not accept the request"])
    assert cl.fake.deleted == [] and rename_audits(cl) == []


async def test_cups_refuses_to_remove_the_old_queue_and_the_new_one_is_taken_back(cl):
    fake = cl.fake
    calls = []

    def handler(request):
        from app.ipp import codec
        m = codec.decode(request.content)
        if m.code == 0x4004:
            name = m.get(codec.TAG_OPERATION, "printer-uri").rsplit("/", 1)[-1]
            calls.append(name)
            if name == "lan":
                ok = codec.IppMessage((2, 0), 0x0500, 1, [codec.Group(codec.TAG_OPERATION, [
                    codec.Attr("attributes-charset", [codec.Val(codec.TAG_CHARSET, "utf-8")]),
                    codec.Attr("attributes-natural-language", [codec.Val(codec.TAG_NATURAL_LANG, "en")])])])
                return httpx.Response(200, content=codec.encode(ok), headers={"content-type": "application/ipp"})
        return fake(request)
    cl.app.state.upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://localhost")
    assert await rename(cl, "lan", "x1") == (303, "/admin/printers", ["CUPS did not accept the request"])
    assert [a[0] for a in fake.added] == ["x1"] and calls == ["lan", "x1"]
    assert sorted(q[0] for q in fake.queues) == ["hp", "lan"] and rename_audits(cl) == []


async def test_success_without_notify(cl):
    status, loc, fl = await rename(cl, "lan", "Office_1")
    assert (status, loc) == (303, "/admin/printers")
    assert fl == ["lan is now called Office_1. Computers and phones set up by hand with the old address must be set up again."]
    name, uri, ppd = cl.fake.added[-1][:3]
    assert (name, uri, ppd) == ("Office_1", "ipp://192.0.2.20:631/ipp/print", "everywhere") and cl.fake.deleted == ["lan"]
    [(action, ttype, tid, detail)] = rename_audits(cl)
    assert (action, ttype, tid) == ("printer.rename", "printer", None)
    assert detail["printer"] == "lan" and detail["new_name"] == "Office_1" and detail["notified_users"] is False and "permission_lists" in detail
    assert broadcast(cl) == []


async def test_success_with_notify_tells_the_other_accounts(cl):
    from tests.conftest import make_user
    make_user(cl.sm, "ann", password="correct horse battery")
    assert (await rename(cl, "lan", "Office_1", notify="1"))[0] == 303
    [(_, _, _, detail)] = rename_audits(cl)
    assert detail["notified_users"] is True
    kinds = broadcast(cl)
    assert kinds and all(k == "printer_renamed" and d == {"old": "lan", "new": "Office_1"} for k, d in kinds)
    with cl.sm() as s:
        from app.db.models import User
        boss = s.scalars(select(User).where(User.username == "boss")).one().id
        assert {n.user_id for n in s.scalars(select(Notice).where(Notice.kind == "printer_renamed"))}.isdisjoint({boss})

