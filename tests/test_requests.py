"""Phases 4-5: requests for more quota / another group, approvals, grants (temporary vs permanent)."""

import base64
import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from spoolsteward.auth import keys
from spoolsteward.config import settings
from spoolsteward.db.models import (
    AdminAudit, Base, Group, JobKind, JobLog, JobState, Metric, Period, QuotaGrant, QuotaRequest,
    RequestScope, RequestStatus, Resource, SubjectType, User,
)
from spoolsteward.db.session import make_engine
from spoolsteward.ipp import codec as c
from spoolsteward.policy import audit, grants
from spoolsteward.policy.engine import active_grants, check_job, effective_limit, effective_rules, window_end
from spoolsteward.policy.rules import set_limit
from tests.conftest import (
    CUPS_TEST_URL, csrf_of_page, make_user, portal_login, requires_cups,
)

GOOD = "correct horse battery"
TZ = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)          # a Wednesday
WEEK_END = datetime(2026, 10, 11, 22, 0, tzinfo=UTC)    # Monday 12 Oct 00:00 in Rome
MONTH_END = datetime(2026, 10, 31, 23, 0, tzinfo=UTC)   # 1 Nov 00:00 in Rome (DST ended on the 25th)


def aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


@pytest.fixture
def org(portal):
    """ann (group staff: 2 jobs/week, 10 pages/month), an admin, and a requestable group 'power'."""
    ann = make_user(portal.sm, "ann", password=GOOD, group="staff")
    boss = make_user(portal.sm, "boss", password=GOOD, admin=True)
    with portal.sm() as s:
        staff = s.scalars(select(Group).where(Group.name == "staff")).one()
        set_limit(s, SubjectType.group, staff.id, Metric.jobs, 2, Period.weekly)
        set_limit(s, SubjectType.group, staff.id, Metric.pages, 10, Period.monthly)
        s.add(Group(name="power", requestable=True))
        s.add(Group(name="admins", requestable=False))
        s.commit()
        set_limit(s, SubjectType.group, s.scalars(select(Group).where(Group.name == "power")).one().id,
                  Metric.jobs, 50, Period.weekly)
        s.commit()
    portal.ann, portal.boss = ann, boss
    return portal


def used_jobs(sm, uid, n, ts=NOW):
    with sm() as s:
        s.add_all([JobLog(ts=ts, user_id=uid, kind=JobKind.print, state=JobState.completed, pages=1, title="x")
                   for _ in range(n)])
        s.commit()


def decide(sm, uid, **kw):
    """ann asks (quota/jobs +3 temporary), boss approves at NOW unless told otherwise."""
    with sm() as s:
        user = s.get(User, uid)
        req = grants.create_quota_request(s, user, kw.pop("metric", "jobs"), kw.pop("amount", 3),
                                          kw.pop("scope", "temporary"), "I need it", kw.pop("asked", NOW))
        return req.id


def approve(sm, rid, boss, now=NOW, **kw):
    with sm() as s:
        return grants.approve_request(s, s.get(QuotaRequest, rid), s.get(User, boss), now, TZ, **kw)


# ---------------------------------------------------------------- the end of a period

def test_window_end_weekly_monthly_and_across_dst_and_year():
    assert window_end(Period.weekly, NOW, TZ) == WEEK_END
    assert window_end(Period.monthly, NOW, TZ) == MONTH_END
    # DST ends on Sun 25 Oct: that week really is 7*24h + 1h long, and still ends on a Monday midnight
    assert window_end(Period.weekly, datetime(2026, 10, 22, 8, tzinfo=UTC), TZ) == datetime(2026, 10, 25, 23, tzinfo=UTC)
    assert window_end(Period.monthly, datetime(2026, 12, 15, tzinfo=UTC), TZ) == datetime(2026, 12, 31, 23, tzinfo=UTC)
    # the very first instant of a window belongs to the NEW window
    assert window_end(Period.weekly, WEEK_END, TZ) == WEEK_END + timedelta(days=7)


# ---------------------------------------------------------------- what the form offers

def test_the_form_offers_only_limited_metrics_and_the_period_the_admin_set(org):
    with org.sm() as s:
        ann = s.get(User, org.ann)
        opts = {o["metric"]: o for o in grants.quota_options(s, ann, NOW, TZ)}
        assert set(opts) == {"jobs", "pages"} and opts["jobs"]["period"] == "weekly" and opts["pages"]["period"] == "monthly"
        assert aware(opts["jobs"]["window_end"]) == WEEK_END and aware(opts["pages"]["window_end"]) == MONTH_END
        set_limit(s, SubjectType.user, ann.id, Metric.jobs, 9, Period.monthly)           # a personal rule wins, with ITS period
        s.commit()
        opts = {o["metric"]: o for o in grants.quota_options(s, ann, NOW, TZ)}
        assert opts["jobs"]["limit"] == 9 and opts["jobs"]["period"] == "monthly"
        set_limit(s, SubjectType.user, ann.id, Metric.jobs, None)
        set_limit(s, SubjectType.group, ann.group_id, Metric.pages, None)
        s.commit()
        assert [o["metric"] for o in grants.quota_options(s, ann, NOW, TZ)] == ["jobs"]  # no pages limit: nothing to ask for


# ---------------------------------------------------------------- creating requests

def test_request_validation(org, monkeypatch):
    monkeypatch.setattr(settings, "request_reason_required", True)
    with org.sm() as s:
        ann = s.get(User, org.ann)
        ok = dict(user=ann, metric="jobs", amount=5, scope="temporary", reason="because", now=NOW)
        for bad, msg in [
            ({"metric": "colour"}, "invalid"), ({"scope": "forever"}, "invalid"),
            ({"amount": 0}, "between"), ({"amount": 10**6}, "between"), ({"amount": "5"}, "between"),
            ({"reason": ""}, "required"), ({"reason": "  \n "}, "required"), ({"reason": "x" * 501}, "too long"),
        ]:
            with pytest.raises(grants.RequestError, match=msg):
                grants.create_quota_request(s, **{**ok, **bad})
        set_limit(s, SubjectType.group, ann.group_id, Metric.pages, None)
        s.commit()
        with pytest.raises(grants.RequestError, match="no pages limit"):
            grants.create_quota_request(s, **{**ok, "metric": "pages"})
        req = grants.create_quota_request(s, **{**ok, "reason": "  needs\x00 it\x07 "})
        assert req.reason == "needs it" and req.status is RequestStatus.pending
        assert aware(req.expires_at) == NOW + timedelta(days=settings.request_ttl_days)


def test_at_most_three_open_requests_and_expired_ones_do_not_count(org):
    with org.sm() as s:
        ann = s.get(User, org.ann)
        reqs = [grants.create_quota_request(s, ann, "jobs", 1, "temporary", f"r{i}", NOW) for i in range(3)]
        with pytest.raises(grants.RequestError, match="3 open"):
            grants.create_quota_request(s, ann, "jobs", 1, "temporary", "one more", NOW)
        grants.cancel_request(s, reqs[0], ann, NOW)                                        # frees a slot
        grants.create_quota_request(s, ann, "jobs", 1, "temporary", "now fits", NOW)
        later = NOW + timedelta(days=settings.request_ttl_days + 1)
        grants.create_quota_request(s, ann, "jobs", 1, "temporary", "after the others expired", later)
        ann.enabled = False
        with pytest.raises(grants.RequestError, match="disabled"):
            grants.create_quota_request(s, ann, "jobs", 1, "temporary", "x", later)


def test_only_the_owner_can_cancel_and_only_while_pending(org):
    other = make_user(org.sm, "bob", password=GOOD)
    rid = decide(org.sm, org.ann)
    with org.sm() as s:
        with pytest.raises(grants.RequestError, match="not your"):
            grants.cancel_request(s, s.get(QuotaRequest, rid), s.get(User, other), NOW)
        grants.cancel_request(s, s.get(QuotaRequest, rid), s.get(User, org.ann), NOW)
        with pytest.raises(grants.RequestError, match="already decided"):
            grants.cancel_request(s, s.get(QuotaRequest, rid), s.get(User, org.ann), NOW)
    with pytest.raises(grants.RequestError, match="already decided"):
        approve(org.sm, rid, org.boss)                                                      # a canceled request can't be approved


# ---------------------------------------------------------------- temporary grants

def test_a_temporary_grant_lasts_until_the_end_of_the_period_the_admin_set(org):
    used_jobs(org.sm, org.ann, 2)
    with org.sm() as s:
        assert "weekly job limit reached (2/2)" in check_job(s, s.get(User, org.ann), Resource.print, NOW, TZ).reason
    rid = decide(org.sm, org.ann, amount=3)
    approve(org.sm, rid, org.boss)
    with org.sm() as s:
        g = s.scalars(select(QuotaGrant)).one()
        assert (g.metric, g.amount, g.scope, aware(g.valid_until), g.request_id) == (Metric.jobs, 3, RequestScope.temporary, WEEK_END, rid)
        ann = s.get(User, org.ann)
        assert check_job(s, ann, Resource.print, NOW, TZ).allowed                           # 2 used of 2+3
        assert active_grants(s, ann, Metric.jobs, WEEK_END - timedelta(seconds=1)) == 3
        assert active_grants(s, ann, Metric.jobs, WEEK_END) == 0                            # gone with the period
        assert active_grants(s, ann, Metric.pages, NOW) == 0                                # other metrics untouched
    used_jobs(org.sm, org.ann, 3)
    with org.sm() as s:
        d = check_job(s, s.get(User, org.ann), Resource.print, NOW, TZ)
        assert not d.allowed and "(5/5)" in d.reason                                        # 2 + 3 granted, all used
        assert check_job(s, s.get(User, org.ann), Resource.print, WEEK_END + timedelta(hours=1), TZ).allowed  # next week


def test_each_metric_ends_with_its_own_period(org):
    approve(org.sm, decide(org.sm, org.ann, metric="jobs", amount=1), org.boss)
    approve(org.sm, decide(org.sm, org.ann, metric="pages", amount=7), org.boss)
    with org.sm() as s:
        by = {g.metric: aware(g.valid_until) for g in s.scalars(select(QuotaGrant))}
        assert by == {Metric.jobs: WEEK_END, Metric.pages: MONTH_END}                       # weekly rule vs monthly rule
        assert effective_limit(s, s.get(User, org.ann), next(r for r in effective_rules(s, s.get(User, org.ann), Resource.print) if r.metric is Metric.pages), NOW) == 17


def test_approving_after_the_boundary_uses_the_window_of_the_approval(org):
    asked = datetime(2026, 10, 11, 21, 50, tzinfo=UTC)                             # Sunday 23:50 in Rome
    rid = decide(org.sm, org.ann, asked=asked)
    decided = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)                             # Monday morning
    approve(org.sm, rid, org.boss, now=decided)
    with org.sm() as s:
        g = s.scalars(select(QuotaGrant)).one()
        assert aware(g.valid_until) == WEEK_END + timedelta(days=7) and aware(g.valid_from) == decided   # not born expired


def test_the_user_never_chooses_the_period(org):
    """The form has no period field, and an invented one is ignored by the server."""
    import inspect
    assert "period" not in inspect.signature(grants.create_quota_request).parameters
    from spoolsteward.admin import me
    assert "period" not in inspect.signature(me.quota_submit).parameters


# ---------------------------------------------------------------- permanent grants

def test_a_permanent_grant_has_no_end_and_applies_to_every_period(org):
    approve(org.sm, decide(org.sm, org.ann, scope="permanent", amount=4), org.boss)
    with org.sm() as s:
        g = s.scalars(select(QuotaGrant)).one()
        assert g.scope is RequestScope.permanent and g.valid_until is None
        ann = s.get(User, org.ann)
        for when in (NOW, WEEK_END + timedelta(days=30), NOW + timedelta(days=400)):
            assert active_grants(s, ann, Metric.jobs, when) == 4


def test_the_admin_can_change_amount_and_kind_when_approving(org):
    rid = decide(org.sm, org.ann, scope="permanent", amount=50)
    approve(org.sm, rid, org.boss, amount=5, scope="temporary", note="only this week")
    with org.sm() as s:
        g = s.scalars(select(QuotaGrant)).one()
        assert (g.amount, g.scope, aware(g.valid_until), g.note) == (5, RequestScope.temporary, WEEK_END, "only this week")
        req = s.get(QuotaRequest, rid)
        assert (req.status, req.decision_note) == (RequestStatus.approved, "only this week")
        assert req.params_json["amount"] == 50 and req.scope is RequestScope.permanent       # what was asked is kept as asked
        detail = s.scalars(select(AdminAudit).where(AdminAudit.action == "request.approve")).one().detail_json
        assert detail["amount"] == 5 and detail["requested_amount"] == 50 and detail["requested_scope"] == "permanent"


def test_invalid_admin_edits_leave_the_request_pending(org):
    rid = decide(org.sm, org.ann)
    for kw in ({"amount": 0}, {"amount": 10**7}, {"scope": "forever"}):
        with pytest.raises((grants.RequestError, ValueError)):
            approve(org.sm, rid, org.boss, **kw)
    with org.sm() as s:
        assert s.get(QuotaRequest, rid).status is RequestStatus.pending and s.scalar(select(func.count(QuotaGrant.id))) == 0


def test_no_grant_when_the_limit_it_extends_no_longer_exists(org):
    rid = decide(org.sm, org.ann)
    with org.sm() as s:
        set_limit(s, SubjectType.group, s.get(User, org.ann).group_id, Metric.jobs, None)
        s.commit()
    with pytest.raises(grants.RequestError, match="nothing to extend"):
        approve(org.sm, rid, org.boss)
    with org.sm() as s:
        assert s.get(QuotaRequest, rid).status is RequestStatus.pending                    # the refused approval changed nothing


# ---------------------------------------------------------------- deciding once

def test_a_request_is_granted_once_even_if_approved_twice_or_rejected_after(org):
    rid = decide(org.sm, org.ann)
    approve(org.sm, rid, org.boss)
    with pytest.raises(grants.RequestError, match="already decided"):
        approve(org.sm, rid, org.boss)
    with org.sm() as s:
        with pytest.raises(grants.RequestError, match="already decided"):
            grants.reject_request(s, s.get(QuotaRequest, rid), s.get(User, org.boss), NOW)
        assert s.scalar(select(func.count(QuotaGrant.id))) == 1


def test_simultaneous_approvals_by_two_admins_grant_exactly_once(tmp_path):
    url = f"sqlite:///{tmp_path}/c.db"
    eng = make_engine(url)
    Base.metadata.create_all(eng)
    sm = sessionmaker(eng, expire_on_commit=False)
    ann = make_user(sm, "ann", password=GOOD, group="staff")
    boss = make_user(sm, "boss", password=GOOD, admin=True)
    with sm() as s:
        set_limit(s, SubjectType.group, s.get(User, ann).group_id, Metric.jobs, 2, Period.weekly)
        s.commit()
    rid = decide(sm, ann)
    results = []

    def worker():
        e = make_engine(url)
        try:
            with sessionmaker(e, expire_on_commit=False)() as s:
                grants.approve_request(s, s.get(QuotaRequest, rid), s.get(User, boss), NOW, TZ)
                results.append("granted")
        except grants.RequestError:
            results.append("refused")
        finally:
            e.dispose()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sorted(results) == ["granted"] + ["refused"] * 7
    with sm() as s:
        assert s.scalar(select(func.count(QuotaGrant.id))) == 1


def test_rejection_expiry_and_a_disabled_user(org):
    rid = decide(org.sm, org.ann)
    with org.sm() as s:
        grants.reject_request(s, s.get(QuotaRequest, rid), s.get(User, org.boss), NOW, "not this month")
        r = s.get(QuotaRequest, rid)
        assert (r.status, r.decision_note, r.decided_by) == (RequestStatus.rejected, "not this month", org.boss)
        assert s.scalar(select(func.count(QuotaGrant.id))) == 0
    old = decide(org.sm, org.ann)
    later = NOW + timedelta(days=settings.request_ttl_days, seconds=1)
    with org.sm() as s:
        assert grants.expire_requests(s, later) == 1 and s.get(QuotaRequest, old).status is RequestStatus.expired
    stale = decide(org.sm, org.ann, asked=NOW)
    with pytest.raises(grants.RequestError, match="expired"):
        approve(org.sm, stale, org.boss, now=later)
    third = decide(org.sm, org.ann)
    with org.sm() as s:
        s.get(User, org.ann).enabled = False
        s.commit()
    with pytest.raises(grants.RequestError, match="disabled"):
        approve(org.sm, third, org.boss)


# ---------------------------------------------------------------- grants given directly, and revoking

def test_direct_grants_and_revocation_are_audited(org):
    with org.sm() as s:
        boss, ann = s.get(User, org.boss), s.get(User, org.ann)
        g = grants.create_grant(s, boss, ann, "pages", 20, "permanent", "project X", NOW, TZ)
        assert g.request_id is None and g.valid_until is None and active_grants(s, ann, Metric.pages, NOW) == 20
        grants.revoke_grant(s, g, boss, NOW)
        assert active_grants(s, ann, Metric.pages, NOW) == 0
        with pytest.raises(grants.RequestError, match="already revoked"):
            grants.revoke_grant(s, g, boss, NOW)
        with pytest.raises(grants.RequestError):
            grants.create_grant(s, boss, ann, "pages", 0, "temporary", "", NOW, TZ)
        assert [a.action for a in s.scalars(select(AdminAudit).order_by(AdminAudit.id))] == ["grant.create", "grant.revoke"]


def test_a_grant_never_touches_the_history(org):
    used_jobs(org.sm, org.ann, 2)
    before = org.sm().execute(select(func.count(JobLog.id))).scalar()
    approve(org.sm, decide(org.sm, org.ann), org.boss)
    with org.sm() as s:
        assert s.scalar(select(func.count(JobLog.id))) == before == 2


def test_grants_count_inside_the_atomic_quota_check(tmp_path):
    """16 simultaneous submissions against limit 3 + grant 2: exactly 5 get in."""
    url = f"sqlite:///{tmp_path}/q.db"
    eng = make_engine(url)
    Base.metadata.create_all(eng)
    sm = sessionmaker(eng, expire_on_commit=False)
    ann = make_user(sm, "ann", password=GOOD, group="staff")
    boss = make_user(sm, "boss", password=GOOD, admin=True)
    with sm() as s:
        set_limit(s, SubjectType.user, ann, Metric.jobs, 3, Period.weekly)
        s.commit()
        grants.create_grant(s, s.get(User, boss), s.get(User, ann), "jobs", 2, "temporary", "", datetime.now(UTC), TZ)
    allowed = []

    def submit():
        e = make_engine(url)
        with sessionmaker(e, expire_on_commit=False)() as s:
            allowed.append(audit.reserve(s, s.get(User, ann), "p", "x", {}, TZ)[0].allowed)
        e.dispose()

    audit._submit_lock = type("NoLock", (), {"__enter__": lambda s: None, "__exit__": lambda s, *a: None})()
    try:
        threads = [threading.Thread(target=submit) for _ in range(16)]
        [t.start() for t in threads]; [t.join() for t in threads]
    finally:
        audit._submit_lock = threading.Lock()
    assert sum(allowed) == 5


# ---------------------------------------------------------------- changing group

def test_only_requestable_other_groups_can_be_asked_for(org, monkeypatch):
    monkeypatch.setattr(settings, "request_reason_required", True)
    with org.sm() as s:
        ann = s.get(User, org.ann)
        gid = {g.name: g.id for g in s.scalars(select(Group))}
        s.get(Group, gid["staff"]).requestable = True                    # asking for the group you are already in
        s.commit()
        for target, msg in ((gid["admins"], "can not be requested"), (999, "can not be requested"), (gid["staff"], "already in")):
            with pytest.raises(grants.RequestError, match=msg):
                grants.create_group_request(s, ann, target, "please", NOW)
        with pytest.raises(grants.RequestError, match="required"):
            grants.create_group_request(s, ann, gid["power"], "", NOW)
        req = grants.create_group_request(s, ann, gid["power"], "new role", NOW)
        assert req.kind.value == "group_change" and req.params_json == {"target_group_id": gid["power"]}


def test_approving_a_group_change_moves_the_user_and_changes_their_limits(org):
    with org.sm() as s:
        power = s.scalars(select(Group).where(Group.name == "power")).one().id
        rid = grants.create_group_request(s, s.get(User, org.ann), power, "promotion", NOW).id
    approve(org.sm, rid, org.boss, note="welcome")
    with org.sm() as s:
        ann = s.get(User, org.ann)
        assert ann.group_id == power
        assert {r.metric: r.limit for r in effective_rules(s, ann, Resource.print)} == {Metric.jobs: 50}   # power's rules, not staff's
        a = s.scalars(select(AdminAudit).where(AdminAudit.action == "request.approve")).one()
        assert a.detail_json["kind"] == "group_change" and a.detail_json["to_group_id"] == power
        with pytest.raises(grants.RequestError):
            grants.approve_request(s, s.get(QuotaRequest, rid), s.get(User, org.boss), NOW, TZ)
    with org.sm() as s:
        assert s.get(User, org.ann).group_id == power                                       # unchanged by the refused second try


def test_a_rejected_group_change_changes_nothing(org):
    with org.sm() as s:
        staff = s.get(User, org.ann).group_id
        rid = grants.create_group_request(s, s.get(User, org.ann), s.scalars(select(Group).where(Group.name == "power")).one().id, "x", NOW).id
        grants.reject_request(s, s.get(QuotaRequest, rid), s.get(User, org.boss), NOW, "no")
        assert s.get(User, org.ann).group_id == staff


# ---------------------------------------------------------------- through the web pages

@pytest.fixture
async def web(org):
    """Two browsers on the same server: ann's and boss's."""
    boss = httpx.AsyncClient(transport=httpx.ASGITransport(app=org.app, client=("10.0.0.9", 1)), base_url="https://gw.test")
    async with boss:
        await portal_login(boss, "boss", GOOD)
        await portal_login(org, "ann", GOOD)
        org.admin = boss
        org.ann_token, org.boss_token = await csrf_of_page(org), await csrf_of_page(boss, "/admin/users")
        yield org


async def test_full_round_trip_in_the_portal_and_the_invented_period_is_ignored(web):
    page = (await web.get("/me/request/quota")).text
    assert 'name="period"' not in page and "weekly" in page and "monthly" in page
    r = await web.post("/me/request/quota", data={"metric": "jobs", "amount": "3", "scope": "temporary", "period": "monthly",
                                                  "reason": "trade fair", "csrf_token": web.ann_token})
    assert r.headers["location"] == "/me"
    html = (await web.admin.get("/admin/requests")).text
    assert "+3 job" in html and "trade fair" in html and 'class="badge">1<' in html          # waiting, and the nav says so
    with web.sm() as s:
        rid = s.scalars(select(QuotaRequest)).one().id
    await web.admin.post(f"/admin/requests/{rid}/approve", data={"amount": "3", "scope": "temporary", "note": "ok",
                                                                 "csrf_token": web.boss_token})
    with web.sm() as s:
        g = s.scalars(select(QuotaGrant)).one()
        until = aware(g.valid_until)
        assert until - aware(g.valid_from) <= timedelta(days=7, hours=1)                    # weekly (the admin's rule), not monthly
    mine = (await web.get("/me")).text
    assert "approved" in mine and "ok" in mine and "3 granted" in mine


async def test_free_text_is_never_interpreted_as_html(web):
    evil = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    await web.post("/me/request/quota", data={"metric": "jobs", "amount": "1", "scope": "permanent", "reason": evil,
                                             "csrf_token": web.ann_token})
    for page in ((await web.get("/me")).text, (await web.admin.get("/admin/requests")).text):
        assert "<script>alert(1)" not in page and "onerror=alert" not in page.replace("&lt;img src=x onerror=alert(2)&gt;", "")
        assert "&lt;script&gt;" in page


async def test_validation_errors_come_back_as_messages_not_crashes(web, monkeypatch):
    monkeypatch.setattr(settings, "request_reason_required", True)
    for data in ({"metric": "jobs", "amount": "abc", "scope": "temporary", "reason": "x"},
                 {"metric": "jobs", "amount": "-4", "scope": "temporary", "reason": "x"},
                 {"metric": "jobs", "amount": "1", "scope": "temporary", "reason": ""},
                 {"metric": "bogus", "amount": "1", "scope": "temporary", "reason": "x"}):
        r = await web.post("/me/request/quota", data={**data, "csrf_token": web.ann_token})
        assert r.status_code == 303 and r.headers["location"] == "/me/request/quota"
    with web.sm() as s:
        assert s.scalar(select(func.count(QuotaRequest.id))) == 0


async def test_users_can_not_see_or_decide_other_peoples_requests_or_admin_pages(web):
    for path in ("/admin/requests", "/admin/actions"):
        assert (await web.get(path)).status_code == 403
    rid = decide(web.sm, web.ann, asked=datetime.now(UTC))   # the page expires requests by the real clock
    r = await web.post(f"/admin/requests/{rid}/approve", data={"csrf_token": web.ann_token})
    assert r.status_code == 403
    make_user(web.sm, "bob", password=GOOD)
    bob = httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app, client=("10.0.0.10", 1)), base_url="https://gw.test")
    async with bob:
        await portal_login(bob, "bob", GOOD)
        assert (await bob.post(f"/me/requests/{rid}/cancel", data={"csrf_token": await csrf_of_page(bob)})).status_code == 404
    with web.sm() as s:
        assert s.get(QuotaRequest, rid).status is RequestStatus.pending


async def test_group_change_through_the_pages(web):
    page = (await web.get("/me/request/group")).text
    assert "power" in page and "admins" not in page                                         # non-requestable groups stay hidden
    with web.sm() as s:
        power = s.scalars(select(Group).where(Group.name == "power")).one().id
    await web.post("/me/request/group", data={"group_id": str(power), "reason": "new job", "csrf_token": web.ann_token})
    with web.sm() as s:
        rid = s.scalars(select(QuotaRequest)).one().id
    assert "move to group power" in (await web.admin.get("/admin/requests")).text
    await web.admin.post(f"/admin/requests/{rid}/approve", data={"csrf_token": web.boss_token})
    with web.sm() as s:
        assert s.get(User, web.ann).group_id == power


async def test_admin_marks_a_group_requestable_in_the_groups_page(web):
    with web.sm() as s:
        gid = s.scalars(select(Group).where(Group.name == "admins")).one().id
    await web.admin.post(f"/admin/groups/{gid}", data={"requestable": "1", "csrf_token": web.boss_token})
    assert "admins" in (await web.get("/me/request/group")).text


async def test_users_can_cancel_their_request_from_their_page(web):
    rid = decide(web.sm, web.ann)
    await web.post(f"/me/requests/{rid}/cancel", data={"csrf_token": web.ann_token})
    with web.sm() as s:
        assert s.get(QuotaRequest, rid).status is RequestStatus.canceled
    assert 'class="badge"' not in (await web.admin.get("/admin/requests")).text


async def test_grants_live_in_the_requests_page_create_and_revoke(web):
    r = await web.admin.post("/admin/grants", data={"username": "ann", "metric": "jobs", "amount": "4", "scope": "temporary",
                                                    "note": "gift", "csrf_token": web.boss_token})
    assert r.headers["location"] == "/admin/requests"
    html = (await web.admin.get("/admin/requests")).text
    assert "+4 jobs" in html and "Active grants" in html and "gift" in html
    assert (await web.admin.get("/admin/grants")).status_code in (404, 405)                    # one page only, no menu entry
    assert 'href="/admin/grants"' not in html
    with web.sm() as s:
        gid = s.scalars(select(QuotaGrant)).one().id
    await web.admin.post(f"/admin/grants/{gid}/revoke", data={"csrf_token": web.boss_token})
    assert "revoked" in (await web.admin.get("/admin/requests")).text


async def test_deleting_ended_grants_keeps_the_active_ones_and_is_logged(web):
    now = datetime.now(UTC)
    with web.sm() as s:
        s.add_all([
            QuotaGrant(user_id=web.ann, metric=Metric.jobs, amount=1, scope=RequestScope.permanent, valid_from=now),
            QuotaGrant(user_id=web.ann, metric=Metric.jobs, amount=2, scope=RequestScope.temporary, valid_from=now - timedelta(days=9),
                       valid_until=now - timedelta(days=1)),
            QuotaGrant(user_id=web.ann, metric=Metric.jobs, amount=3, scope=RequestScope.permanent, valid_from=now, revoked_at=now),
            QuotaGrant(user_id=web.ann, metric=Metric.jobs, amount=4, scope=RequestScope.temporary, valid_from=now,
                       valid_until=now + timedelta(days=3)),
        ])
        s.commit()
    assert "Delete ended grants" in (await web.admin.get("/admin/requests")).text
    await web.admin.post("/admin/grants/purge", data={"csrf_token": web.boss_token})
    with web.sm() as s:
        assert sorted(g.amount for g in s.scalars(select(QuotaGrant))) == [1, 4]
        assert s.scalars(select(AdminAudit).where(AdminAudit.action == "grant.purge")).one().detail_json == {"count": 2, "quota": 2, "permission": 0}
    assert "Delete ended grants" not in (await web.admin.get("/admin/requests")).text
    await web.admin.post("/admin/grants/purge", data={"csrf_token": web.boss_token})          # nothing left: nothing logged
    with web.sm() as s:
        assert len(s.scalars(select(AdminAudit).where(AdminAudit.action == "grant.purge")).all()) == 1


async def test_users_can_not_delete_grants(web):
    assert (await web.post("/admin/grants/purge", data={"csrf_token": web.ann_token})).status_code == 403


# ---------------------------------------------------------------- the printing side

@pytest.fixture
async def ipp(web, monkeypatch):
    monkeypatch.setattr(settings, "ipp_auth", "keys")
    web.app.state.upstream = httpx.AsyncClient(base_url=CUPS_TEST_URL, timeout=10)
    with web.sm() as s:
        _, key = keys.create_key(s, s.get(User, web.ann), "t", 30)
    web.key = key
    yield web
    await web.app.state.upstream.aclose()


def pj(n=1):
    r = c.make_request(c.PRINT_JOB, n, printer_uri="ipp://gw.test/printers/virtual", job_name=f"d{n}.txt", document_format="text/plain")
    r.data = b"x\n"
    return r


async def ipp_post(cl, req):
    auth = base64.b64encode(f"ann:{cl.key}".encode()).decode()
    return c.decode((await cl.post("/printers/virtual", content=c.encode(req),
                                   headers={"Authorization": f"Basic {auth}", "Content-Type": "application/ipp"})).content)


@requires_cups
async def test_a_refusal_tells_the_user_where_to_ask_and_a_grant_unblocks_printing(ipp):
    for i in range(1, 3):
        assert (await ipp_post(ipp, pj(i))).code < 0x0100
    denied = await ipp_post(ipp, pj(9))
    msg = denied.get(c.TAG_OPERATION, "status-message")
    assert denied.code == c.STATUS_NOT_POSSIBLE and "weekly job limit reached (2/2)" in msg
    assert "https://gw.test/me" in msg                                                      # where to ask for more
    with ipp.sm() as s:
        grants.create_grant(s, s.get(User, ipp.boss), s.get(User, ipp.ann), "jobs", 1, "temporary", "", datetime.now(UTC), TZ)
    assert (await ipp_post(ipp, pj(10))).code < 0x0100                                      # one more allowed...
    assert (await ipp_post(ipp, pj(11))).code == c.STATUS_NOT_POSSIBLE                      # ...and not two
    with ipp.sm() as s:
        assert s.scalar(select(func.count(JobLog.id)).where(JobLog.state == JobState.accepted)) == 3


# ---------------------------------------------------------------- optional reason, unlimited quota in the grant form

def test_the_reason_is_optional_by_default(org):
    assert settings.request_reason_required is False
    with org.sm() as s:
        req = grants.create_quota_request(s, s.get(User, org.ann), "jobs", 5, "temporary", "  ", NOW)
        assert req.reason == ""


async def test_forms_say_if_the_reason_is_required(web, monkeypatch):
    for url in ("/me/request/quota", "/me/request/group", "/me/request/permission"):
        page = (await web.get(url)).text
        assert 'name="reason"' not in page or "required" not in page.split('name="reason"')[1].split(">")[0], url
    monkeypatch.setattr(settings, "request_reason_required", True)
    assert "required" in (await web.get("/me/request/quota")).text.split('name="reason"')[1].split(">")[0]


async def test_lacks_lists_only_the_metrics_that_limit_the_user(web):
    me = (await web.admin.get("/admin/requests/lacks?username=ann")).json()
    assert "jobs" in me["metrics"]
    with web.sm() as s:
        set_limit(s, SubjectType.group, s.get(User, web.ann).group_id, Metric.jobs, None)
        set_limit(s, SubjectType.group, s.get(User, web.ann).group_id, Metric.pages, None)
        s.commit()
    assert (await web.admin.get("/admin/requests/lacks?username=ann")).json()["metrics"] == []
