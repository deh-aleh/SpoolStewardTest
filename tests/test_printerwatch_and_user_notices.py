"""Printer problems (jam, paper, ink) reach the administrators and the people waiting; users hear about changes to their
account; an administrator has ONE bell."""

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from spoolsteward import notices, printerwatch
from spoolsteward.config import settings
from spoolsteward.db.models import Group, JobKind, JobLog, JobState, Notice, QuotaGrant, User
from tests.conftest import csrf_of_page, make_user, portal_login

GOOD = "correct horse battery"


def uid(cl, name):
    with cl.sm() as s:
        return s.scalars(select(User).where(User.username == name)).one().id


def mine(cl, name, kind=None):
    with cl.sm() as s:
        return [n for n in s.scalars(select(Notice).where(Notice.user_id == uid(cl, name)).order_by(Notice.id))
                if n.dismissed_at is None and (kind is None or n.kind == kind)]


def queue(name="hp", reasons=(), ink=(), uri="usb://HP/x"):
    return {"name": name, "info": "Office HP", "device_uri": uri, "reasons": list(reasons), "ink": list(ink)}


def test_what_counts_as_a_problem():
    p = printerwatch.problems_of
    assert p(queue(reasons=["media-jam-error"])) == {"jam": "media-jam-error"}
    assert set(p(queue(reasons=["media-empty-error", "media-needed-warning"]))) == {"paper"}
    assert set(p(queue(reasons=["toner-empty-error"]))) == {"ink"}
    assert p(queue(ink=[{"name": "Black", "level": 0, "low": True}, {"name": "Cyan", "level": 8, "low": True}])) == {"ink": "Black"}
    assert p(queue(ink=[{"name": "Cyan", "level": 8, "low": True}, {"name": "Black", "level": 90, "low": False}])) == {"ink_low": "Cyan"}
    assert p(queue(ink=[{"name": "Black", "level": -1, "low": False}], reasons=["none", "cups-waiting-for-job-completed"])) == {}
    assert printerwatch.evaluate([queue(reasons=["media-jam"], uri="cups-pdf:/")]) == {}      # test queues are not printers


@pytest.fixture
def boss(portal):
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    make_user(portal.sm, "ann", password=GOOD)
    make_user(portal.sm, "bob", password=GOOD)
    with portal.sm() as s:
        s.add(JobLog(ts=datetime.now(UTC), user_id=uid(portal, "ann"), user_name="ann", kind=JobKind.print, printer="hp",
                     state=JobState.accepted, cups_job_id=5))
        s.add(JobLog(ts=datetime.now(UTC), user_id=uid(portal, "bob"), user_name="bob", kind=JobKind.print, printer="hp",
                     state=JobState.completed, cups_job_id=4))
        s.commit()
    printerwatch._streak.clear()
    return portal


def run(cl, queues, times=1):
    for _ in range(times):
        printerwatch.apply(cl.sm, printerwatch.evaluate(queues), [q["name"] for q in queues])


def test_a_problem_is_reported_once_confirmed_to_admins_and_to_who_waits(boss):
    jam = [queue(reasons=["media-jam-error"])]
    run(boss, jam)
    assert boss.sm().scalars(select(Notice)).all() == []                       # seen once: not yet
    run(boss, jam)
    with boss.sm() as s:
        admin_rows = s.scalars(select(Notice).where(Notice.user_id.is_(None))).all()
    assert [(n.kind, n.key, n.detail_json["problem"]) for n in admin_rows] == [("printer_problem", "hp:jam", "jam")]
    assert [n.detail_json["printer"] for n in mine(boss, "ann")] == ["Office HP"]  # the one whose print waits
    assert mine(boss, "bob") == [] and mine(boss, "boss") == []                # a finished print / nobody else
    run(boss, jam, times=3)
    assert len(mine(boss, "ann")) == 1                                         # not repeated while it lasts
    run(boss, [queue()], times=1)                                              # solved: everything closes
    with boss.sm() as s:
        assert all(n.dismissed_at is not None for n in s.scalars(select(Notice)))
    assert mine(boss, "ann") == []


def test_users_are_not_told_when_the_option_is_off_and_low_ink_is_for_admins_only(boss, monkeypatch):
    monkeypatch.setattr(settings, "printer_alerts_users", False)
    run(boss, [queue(reasons=["media-empty"])], times=2)
    assert mine(boss, "ann") == []
    with boss.sm() as s:
        assert [n.key for n in s.scalars(select(Notice).where(Notice.user_id.is_(None)))] == ["hp:paper"]
    monkeypatch.setattr(settings, "printer_alerts_users", True)
    run(boss, [queue(ink=[{"name": "Cyan", "level": 5, "low": True}])], times=2)
    assert mine(boss, "ann") == []                                             # "running low" is for the administrators
    with boss.sm() as s:
        assert "hp:ink_low" in [n.key for n in s.scalars(select(Notice).where(Notice.user_id.is_(None), Notice.dismissed_at.is_(None)))]


async def test_an_administrator_has_one_bell_with_both_kinds_of_notices(boss):
    notices.notify_user(boss.sm, uid(boss, "boss"), "key_revoked", {"label": "old laptop", "prefix": "abc"})
    notices.report(boss.sm, "escl_anonymous_refused", "10.0.0.8")
    await portal_login(boss, "boss", GOOD)
    page = (await boss.get("/admin/jobs")).text
    assert page.count('class="bell iconbtn') == 1 and 'class="belln">2<' in page
    assert (await boss.get("/me/notices", follow_redirects=False)).headers["location"] == "/admin/notices"
    both = (await boss.get("/admin/notices")).text
    assert "old laptop" in both and "10.0.0.8" in both and "For you" in both
    assert 'class="belln"' not in (await boss.get("/me")).text                 # opening the page counted as seeing both
    token = re.search(r'name="csrf_token" value="([^"]+)"', both).group(1)
    nid = mine(boss, "boss")[0].id
    assert (await boss.post(f"/me/notices/{nid}/dismiss", data={"csrf_token": token})).status_code == 303
    assert mine(boss, "boss") == []


async def test_users_are_told_about_group_admin_grants_and_requests(boss):
    await portal_login(boss, "boss", GOOD)
    token = await csrf_of_page(boss, "/admin/users")
    with boss.sm() as s:
        s.add(Group(name="staff"))
        s.commit()
        staff = s.scalars(select(Group).where(Group.name == "staff")).one().id
    ann = uid(boss, "ann")
    form = {"csrf_token": token, "group_id": staff, "enabled": "1", "provider": "local", "period": "weekly"}
    assert (await boss.post(f"/admin/users/{ann}", data=form)).status_code == 303
    n = mine(boss, "ann", "group_changed")
    assert len(n) == 1 and n[0].detail_json["new"] == "staff"
    await boss.post(f"/admin/users/{ann}", data=form)                          # nothing changed: nothing said
    assert len(mine(boss, "ann", "group_changed")) == 1
    await boss.post(f"/admin/users/{ann}", data={**form, "is_admin": "1"})
    assert len(mine(boss, "ann", "admin_on")) == 1 and len(mine(boss, "ann", "group_changed")) == 1
    await boss.post(f"/admin/users/{ann}", data=form)
    off = mine(boss, "ann", "admin_off")
    assert len(off) == 1 and off[0].detail_json["group"] == "staff"
    from spoolsteward.db.models import Metric, SubjectType
    from spoolsteward.policy.rules import set_limit
    with boss.sm() as s:
        set_limit(s, SubjectType.user, ann, Metric.pages, 10)
        s.commit()
    await boss.post("/admin/grants", data={"csrf_token": token, "username": "ann", "metric": "pages", "amount": "20",
                                           "scope": "temporary", "note": "thesis"})
    g = mine(boss, "ann", "grant_given")
    assert len(g) == 1 and g[0].detail_json["amount"] == 20
    with boss.sm() as s:
        gid = s.scalars(select(QuotaGrant)).one().id
    await boss.post(f"/admin/grants/{gid}/revoke", data={"csrf_token": token})
    assert len(mine(boss, "ann", "grant_revoked")) == 1
    async with __import__("httpx").AsyncClient(transport=__import__("httpx").ASGITransport(app=boss.app, client=("10.0.0.9", 1)),
                                               base_url="https://gw.test") as a:
        await a.post("/login", data={"username": "ann", "password": GOOD})
        text = (await a.get("/me/notices")).text
    for part in ("moved you from the group", "made you an administrator", "removed your administrator rights",
                 "gave you extra quota", "withdrew your extra quota"):
        assert part in text, part
