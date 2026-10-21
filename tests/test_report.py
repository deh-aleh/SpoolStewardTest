"""Usage report: counts, order, excluded groups, detail by printer, CSV safety, PDF, dates."""

import io
from datetime import UTC, datetime, timedelta

import pytest
from pypdf import PdfReader
from sqlalchemy import select

from spoolsteward.db.models import Group, JobKind, JobLog, JobState, User
from tests.conftest import make_user, portal_login

GOOD = "correct horse battery"


def add(cl, name, printer, pages, state=JobState.completed, days_ago=1, kind=JobKind.print):
    with cl.sm() as s:
        uid = s.scalars(select(User).where(User.username == name)).first()
        s.add(JobLog(ts=datetime.now(UTC) - timedelta(days=days_ago), user_id=uid.id if uid else None,
                     user_name=None if uid else name, kind=kind, printer=printer, state=state, pages=pages))
        s.commit()


@pytest.fixture
async def rep(portal):
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    make_user(portal.sm, "ann", password=GOOD, group="staff")
    make_user(portal.sm, "bob", password=GOOD, group="interns")
    make_user(portal.sm, "=evil", password=GOOD, group="interns")
    add(portal, "ann", "hp", 10)
    add(portal, "ann", "brother", 5)
    add(portal, "bob", "hp", 30)
    add(portal, "=evil", "hp", 1)
    add(portal, "ghost", "hp", 7)                               # an account that was deleted: the name stays on the row
    add(portal, "ann", "hp", 99, state=JobState.failed)         # did not print
    add(portal, "ann", "hp", 99, days_ago=90)                   # outside the period
    add(portal, "ann", "scanner", 40, kind=JobKind.scan)        # scans are not prints
    await portal_login(portal, "boss", GOOD)
    return portal


def groups(cl, *names):
    with cl.sm() as s:
        return [g.id for g in s.scalars(select(Group).where(Group.name.in_(names)))]


async def test_the_page_ranks_users_by_pages(rep):
    page = (await rep.get("/admin/report")).text
    order = [page.index(f"<b>{n}</b>") for n in ("bob", "ann", "ghost")]
    assert order == sorted(order)
    assert "<b>ann</b></td><td>staff</td><td>2</td><td>15</td>" in page   # 10 + 5, the failed and the old ones left out
    assert "4 users" in page


async def test_excluding_a_group_and_the_top_n(rep):
    ids = groups(rep, "interns")
    page = (await rep.get("/admin/report", params={"exclude": ids})).text
    assert "<b>bob</b>" not in page and "<b>ann</b>" in page and "<b>ghost</b>" in page   # a deleted user has no group to leave out
    top = (await rep.get("/admin/report", params={"top": "1"})).text
    assert "<b>bob</b>" in top and "<b>ann</b>" not in top and "muted" in top


async def test_csv_summary_and_detail_and_formula_safety(rep):
    r = await rep.get("/admin/report/download", params={"format": "csv", "top": ""})
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.splitlines()
    assert lines[0] == "rank,user,group,jobs,pages" and lines[1] == "1,bob,interns,1,30" and "2,ann,staff,2,15" in lines
    assert "'=evil" in r.text and ",=evil" not in r.text
    d = await rep.get("/admin/report/download", params={"format": "csv", "detail": "1", "top": ""})
    rows = d.text.splitlines()
    assert rows[0] == "rank,user,group,printer,jobs,pages" and "2,ann,staff,hp,1,10" in rows and "2,ann,staff,brother,1,5" in rows


async def test_pdf_is_a_real_pdf_with_the_numbers_and_survives_odd_names(rep):
    make_user(rep.sm, "мария", password=GOOD)
    add(rep, "мария", "hp", 3)
    r = await rep.get("/admin/report/download", params={"format": "pdf", "detail": "1", "top": ""})
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
    assert "Print report" in text and "ann" in text and "brother" in text and "30" in text


async def test_bad_dates_and_numbers_do_not_crash(rep):
    for params in ({"start": "31/10/2026"}, {"start": "2026-10-10", "end": "2026-10-01"}, {"top": "abc"}, {"top": "0"}):
        assert (await rep.get("/admin/report", params=params)).status_code == 200
        assert (await rep.get("/admin/report/download", params=params, follow_redirects=False)).status_code == 303


async def test_only_administrators(rep):
    make_user(rep.sm, "carl", password=GOOD)
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=rep.app, client=("10.0.0.9", 1)), base_url="https://gw.test") as c:
        await c.post("/login", data={"username": "carl", "password": GOOD})
        for path in ("/admin/report", "/admin/report/download?format=csv"):
            assert (await c.get(path, follow_redirects=False)).status_code in (303, 403)
