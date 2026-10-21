"""Phase 4: asking for a permission (colour, one-sided, a printer, any hours), approving it for good or until a date,
handing one out directly, revoking it, the notices, the end of a temporary one, and cleaning up."""

import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from spoolsteward import keywatch
from spoolsteward.db.migrate import upgrade
from spoolsteward.db.models import (
    AdminAudit, Notice, PermissionGrant, QuotaGrant, QuotaRequest, RequestKind, RequestStatus, SubjectType, User,
)
from spoolsteward.policy import grants, permissions
from tests.conftest import csrf_of_page, make_user, portal_login

GOOD = "correct horse battery"
TZ = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


@pytest.fixture
def org(portal):
    """ann without colour, without one-sided, only on printer "bw", only 08:00-17:00 on weekdays; boss (administrator)."""
    portal.ann = make_user(portal.sm, "ann", password=GOOD, group="staff")
    portal.boss = make_user(portal.sm, "boss", password=GOOD, admin=True)
    with portal.sm() as s:
        permissions.set_rule(s, SubjectType.user, portal.ann, color=False, one_sided=False, printers_mode="list", printers=["bw"],
                             hours_mode="window", hours_days="01234", hours_start=480, hours_end=1020)
        s.commit()
    return portal


def ask(sm, uid, perm="color", printer=None, reason="because", now=NOW):
    with sm() as s:
        return grants.create_permission_request(s, s.get(User, uid), perm, printer, reason, now).id


def approve(sm, rid, boss, now=NOW, **kw):
    with sm() as s:
        return grants.approve_request(s, s.get(QuotaRequest, rid), s.get(User, boss), now, TZ, **kw)


def perms_of(sm, uid, now=NOW):
    with sm() as s:
        return permissions.effective(s, s.get(User, uid), now)


# ---------------------------------------------------------------- the rules

def test_approved_for_good_widens_the_permissions_from_then_on(org):
    assert not perms_of(org.sm, org.ann).color
    rid = ask(org.sm, org.ann)
    assert not perms_of(org.sm, org.ann).color                                   # asking changes nothing
    g = approve(org.sm, rid, org.boss, note="ok")
    assert g.perm == "color" and g.valid_until is None and g.request_id == rid
    later = NOW + timedelta(days=900)
    assert perms_of(org.sm, org.ann, later).color and not perms_of(org.sm, org.ann, later).one_sided
    with org.sm() as s:
        assert s.get(QuotaRequest, rid).status is RequestStatus.approved


def test_each_kind_of_permission_widens_only_what_it_names(org):
    for perm, printer in (("one_sided", None), ("hours", None), ("printer", "Color")):
        approve(org.sm, ask(org.sm, org.ann, perm, printer), org.boss)
    p = perms_of(org.sm, org.ann)
    assert p.one_sided and p.window is None and p.printers == {"bw", "color"} and not p.color


def test_a_printer_grant_changes_nothing_when_no_list_applies(org):
    other = make_user(org.sm, "free", password=GOOD)
    with org.sm() as s:
        permissions.set_rule(s, SubjectType.user, other, color=False)
        s.add(PermissionGrant(user_id=other, perm="printer", printer="x", valid_from=NOW))
        s.commit()
    assert perms_of(org.sm, other).printers is None


def test_until_a_date_covers_the_whole_last_day_in_the_portal_time_zone(org):
    rid = ask(org.sm, org.ann)
    g = approve(org.sm, rid, org.boss, until=date(2026, 11, 30))
    assert aware(g.valid_until) == datetime(2026, 11, 30, 23, 0, tzinfo=UTC)      # 1 Dec 00:00 in Rome (CET)
    assert perms_of(org.sm, org.ann, datetime(2026, 11, 30, 22, 59, tzinfo=UTC)).color
    assert not perms_of(org.sm, org.ann, datetime(2026, 11, 30, 23, 0, tzinfo=UTC)).color
    assert not perms_of(org.sm, org.ann, NOW - timedelta(days=1)).color                    # not yet started


def test_a_date_in_the_past_is_refused_and_the_request_stays_pending(org):
    rid = ask(org.sm, org.ann)
    with pytest.raises(grants.RequestError, match="future"):
        approve(org.sm, rid, org.boss, until=date(2026, 10, 6))
    with org.sm() as s:
        assert s.get(QuotaRequest, rid).status is RequestStatus.pending and not s.scalars(select(PermissionGrant)).all()
    approve(org.sm, rid, org.boss, until=date(2026, 10, 7))                                 # today is fine: valid until tonight


def test_double_approval_grants_once(org):
    rid = ask(org.sm, org.ann)
    approve(org.sm, rid, org.boss)
    with pytest.raises(grants.RequestError, match="already decided"):
        approve(org.sm, rid, org.boss)
    with org.sm() as s:
        assert len(s.scalars(select(PermissionGrant)).all()) == 1


def test_reject_grants_nothing(org):
    rid = ask(org.sm, org.ann)
    with org.sm() as s:
        grants.reject_request(s, s.get(QuotaRequest, rid), s.get(User, org.boss), NOW, "no")
        assert s.get(QuotaRequest, rid).status is RequestStatus.rejected and not s.scalars(select(PermissionGrant)).all()
    assert not perms_of(org.sm, org.ann).color


def test_the_open_request_limit_counts_permission_requests(org, monkeypatch):
    from spoolsteward.config import settings
    monkeypatch.setattr(settings, "max_open_requests", 2)
    ask(org.sm, org.ann, "color")
    ask(org.sm, org.ann, "one_sided")
    with pytest.raises(grants.RequestError, match="open requests"):
        ask(org.sm, org.ann, "hours")


def test_asking_for_what_you_have_or_already_asked_is_refused(org):
    other = make_user(org.sm, "free", password=GOOD)
    with pytest.raises(grants.RequestError, match="already have"):
        ask(org.sm, other, "color")
    with pytest.raises(grants.RequestError, match="already have"):
        ask(org.sm, org.ann, "printer", "BW")                                              # case does not matter
    ask(org.sm, org.ann, "color")
    with pytest.raises(grants.RequestError, match="already asked"):
        ask(org.sm, org.ann, "color")
    ask(org.sm, org.ann, "printer", "color")                                               # another subject: fine
    approve(org.sm, 1, org.boss)
    with pytest.raises(grants.RequestError, match="already have"):                       # granted since
        ask(org.sm, org.ann, "color", now=NOW + timedelta(hours=1))


@pytest.mark.parametrize("perm,printer", [("nope", None), ("", None), ("printer", None), ("printer", ""), ("printer", "a b"),
                                          ("printer", "../x"), ("printer", "x" * 128), ("printer", "a;b")])
def test_invalid_permission_requests_are_refused(org, perm, printer):
    with pytest.raises(grants.RequestError):
        ask(org.sm, org.ann, perm, printer)


def test_the_reason_is_required(org, monkeypatch):
    from spoolsteward.config import settings
    monkeypatch.setattr(settings, "request_reason_required", True)
    with pytest.raises(grants.RequestError, match="reason"):
        ask(org.sm, org.ann, reason="  ")


def test_the_printer_name_is_stored_in_lower_case_and_the_params_have_two_keys(org):
    rid = ask(org.sm, org.ann, "printer", "  Color ")
    with org.sm() as s:
        r = s.get(QuotaRequest, rid)
        assert r.kind is RequestKind.permission and r.params_json == {"perm": "printer", "printer": "color"}
    ask_color = ask(org.sm, org.ann, "color")
    with org.sm() as s:
        assert s.get(QuotaRequest, ask_color).params_json == {"perm": "color", "printer": None}


def test_the_emergency_administrator_has_everything_so_has_nothing_to_ask(portal, monkeypatch):
    from spoolsteward.config import settings
    monkeypatch.setattr(settings, "bootstrap_admin_username", "root1")
    root = make_user(portal.sm, "root1", password=GOOD, admin=True)
    with pytest.raises(grants.RequestError, match="already have"):
        ask(portal.sm, root, "color")


# ---------------------------------------------------------------- direct grants, revoking, ending, cleaning

def test_direct_grant_and_revoke_are_conditional_and_logged(org):
    with org.sm() as s:
        boss, ann = s.get(User, org.boss), s.get(User, org.ann)
        g = grants.create_permission_grant(s, boss, ann, "printer", "Color", date(2026, 12, 31), "loan", NOW, TZ)
        assert g.request_id is None and g.printer == "color" and g.note == "loan"
        assert permissions.effective(s, ann, NOW).printers == {"bw", "color"}
        grants.revoke_permission_grant(s, g, boss, NOW)
        with pytest.raises(grants.RequestError, match="already revoked"):
            grants.revoke_permission_grant(s, g, boss, NOW)
        assert permissions.effective(s, ann, NOW).printers == {"bw"}
        assert [a.action for a in s.scalars(select(AdminAudit).order_by(AdminAudit.id))] == ["grant.create", "grant.revoke"]


def test_direct_grant_refuses_bad_input(org):
    with org.sm() as s:
        boss, ann = s.get(User, org.boss), s.get(User, org.ann)
        for perm, printer, until in (("zzz", None, None), ("printer", "no good", None), ("color", None, date(2020, 1, 1))):
            with pytest.raises(grants.RequestError):
                grants.create_permission_grant(s, boss, ann, perm, printer, until, "", NOW, TZ)
        assert not s.scalars(select(PermissionGrant)).all()


def test_notify_expired_tells_once_and_never_for_revoked_or_permanent_ones(org):
    with org.sm() as s:
        boss, ann = s.get(User, org.boss), s.get(User, org.ann)
        short = grants.create_permission_grant(s, boss, ann, "color", None, date(2026, 10, 8), "", NOW, TZ)
        revoked = grants.create_permission_grant(s, boss, ann, "hours", None, date(2026, 10, 8), "", NOW, TZ)
        grants.create_permission_grant(s, boss, ann, "one_sided", None, None, "", NOW, TZ)
        grants.revoke_permission_grant(s, revoked, boss, NOW)
    assert permissions.notify_expired(org.sm, NOW + timedelta(hours=5)) == 0               # still valid
    later = NOW + timedelta(days=3)
    assert permissions.notify_expired(org.sm, later) == 1
    assert permissions.notify_expired(org.sm, later) == 0                                  # once
    assert keywatch.run_once(org.sm, later) == (0, 0)
    with org.sm() as s:
        got = s.scalars(select(Notice).where(Notice.kind == "permission_expired")).all()
        assert len(got) == 1 and got[0].user_id == org.ann and got[0].detail_json == {"perm": "color", "printer": None}
        assert s.get(PermissionGrant, short.id).expiry_notified_at is not None


def test_purge_deletes_ended_and_revoked_permission_grants_and_logs_both_kinds(org):
    from spoolsteward.db.models import Metric, RequestScope
    with org.sm() as s:
        boss = s.get(User, org.boss)
        s.add_all([
            PermissionGrant(user_id=org.ann, perm="color", valid_from=NOW - timedelta(days=9), valid_until=NOW - timedelta(days=1)),
            PermissionGrant(user_id=org.ann, perm="hours", valid_from=NOW, revoked_at=NOW),
            PermissionGrant(user_id=org.ann, perm="one_sided", valid_from=NOW),
            PermissionGrant(user_id=org.ann, perm="printer", printer="x", valid_from=NOW, valid_until=NOW + timedelta(days=3)),
            QuotaGrant(user_id=org.ann, metric=Metric.jobs, amount=1, scope=RequestScope.permanent, valid_from=NOW, revoked_at=NOW),
        ])
        s.commit()
        assert grants.purge_ended(s, boss, NOW) == 3
        assert sorted(g.perm for g in s.scalars(select(PermissionGrant))) == ["one_sided", "printer"]
        assert s.scalars(select(AdminAudit).where(AdminAudit.action == "grant.purge")).one().detail_json == \
            {"count": 3, "quota": 1, "permission": 2}
        assert grants.purge_ended(s, boss, NOW) == 0


# ---------------------------------------------------------------- the pages

@pytest.fixture
async def web(org):
    """Two browsers on the same server: ann's and boss's, with two printers in CUPS."""
    org.app.state.queues_cache = (time.monotonic(), [
        {"name": "bw", "info": "", "model": "", "device_uri": "ipp://p/1", "virtual": False, "color": False, "duplex": False, "media": []},
        {"name": "color", "info": "", "model": "", "device_uri": "ipp://p/2", "virtual": False, "color": True, "duplex": True, "media": []}])
    boss = httpx.AsyncClient(transport=httpx.ASGITransport(app=org.app, client=("10.0.0.9", 1)), base_url="https://gw.test")
    async with boss:
        await portal_login(boss, "boss", GOOD)
        await portal_login(org, "ann", GOOD)
        org.admin = boss
        org.ann_token, org.boss_token = await csrf_of_page(org), await csrf_of_page(boss, "/admin/users")
        yield org


def notices_of(sm, uid, kind):
    with sm() as s:
        return [n.detail_json for n in s.scalars(select(Notice).where(Notice.user_id == uid, Notice.kind == kind).order_by(Notice.id))]


async def test_profile_offers_to_ask_only_for_what_is_missing(web):
    html = (await web.get("/me")).text
    for perm in ("color", "one_sided", "hours", "printer"):
        assert f"/me/request/permission?perm={perm}" in html
    form = (await web.get("/me/request/permission?perm=printer")).text
    assert "colour printing" in form and 'value="printer" selected' in form and "<option >color</option>" in form
    assert ">bw</option>" not in form                                                          # the printer ann may already use is not offered
    with web.sm() as s:
        permissions.set_rule(s, SubjectType.user, web.ann)       # no rule: everything allowed
        s.commit()
    html = (await web.get("/me")).text
    assert "/me/request/permission" not in html
    assert "nothing to ask for" in (await web.get("/me/request/permission")).text


async def test_round_trip_request_approve_until_a_date_and_the_notices(web):
    r = await web.post("/me/request/permission", data={"perm": "printer", "printer": "COLOR", "reason": "posters", "csrf_token": web.ann_token})
    assert r.headers["location"] == "/me"
    assert "printer color" in (await web.get("/me")).text                                  # in the user's own list, waiting
    admin_html = (await web.admin.get("/admin/requests")).text
    assert "printer color" in admin_html and "posters" in admin_html and 'name="until"' in admin_html
    with web.sm() as s:
        rid = s.scalars(select(QuotaRequest)).one().id
    day = (datetime.now(UTC) + timedelta(days=20)).astimezone(TZ).date()
    await web.admin.post(f"/admin/requests/{rid}/approve", data={"duration": "until", "until": day.isoformat(), "note": "ok",
                                                                 "csrf_token": web.boss_token})
    with web.sm() as s:
        g = s.scalars(select(PermissionGrant)).one()
        assert (g.perm, g.printer, g.granted_by, g.request_id) == ("printer", "color", web.boss, rid) and g.valid_until is not None
        assert "color" in permissions.effective(s, s.get(User, web.ann)).printers
    assert notices_of(web.sm, web.ann, "request_approved") == [
        {"what": "permission", "perm": "printer", "printer": "color", "until": day.isoformat(), "note": "ok"}]
    html = (await web.get("/me/notices")).text
    assert "Your request for a permission was approved: printer color" in html and day.isoformat() in html
    assert f"until {day.isoformat()}" in (await web.get("/me")).text
    active = (await web.admin.get("/admin/requests")).text
    assert "+ printer color" in active and day.isoformat() in active and "/admin/permission-grants/" in active
    assert "printer color" in (await web.admin.get(f"/admin/users/{web.ann}/edit")).text


async def test_approving_with_a_missing_or_past_date_fails_visibly_and_changes_nothing(web):
    await web.post("/me/request/permission", data={"perm": "color", "reason": "r", "csrf_token": web.ann_token})
    with web.sm() as s:
        rid = s.scalars(select(QuotaRequest)).one().id
    for data in ({"duration": "until", "until": ""}, {"duration": "until", "until": "garbage"}, {"duration": "until", "until": "2001-01-01"}):
        r = await web.admin.post(f"/admin/requests/{rid}/approve", data={**data, "csrf_token": web.boss_token})
        assert r.status_code == 303
        with web.sm() as s:
            assert s.get(QuotaRequest, rid).status is RequestStatus.pending and not s.scalars(select(PermissionGrant)).all()
    await web.admin.post(f"/admin/requests/{rid}/approve", data={"csrf_token": web.boss_token})        # no choice sent: permanent
    with web.sm() as s:
        assert s.scalars(select(PermissionGrant)).one().valid_until is None


async def test_reject_through_the_page_tells_the_user(web):
    await web.post("/me/request/permission", data={"perm": "color", "reason": "r", "csrf_token": web.ann_token})
    with web.sm() as s:
        rid = s.scalars(select(QuotaRequest)).one().id
    await web.admin.post(f"/admin/requests/{rid}/reject", data={"note": "not now", "csrf_token": web.boss_token})
    assert notices_of(web.sm, web.ann, "request_rejected") == [{"note": "not now", "what": "permission", "perm": "color", "printer": None}]
    assert "Your request for a permission was rejected: colour printing" in (await web.get("/me/notices")).text
    with web.sm() as s:
        assert not s.scalars(select(PermissionGrant)).all()


async def test_user_can_cancel_a_permission_request_and_the_form_validates(web, monkeypatch):
    from spoolsteward.config import settings
    monkeypatch.setattr(settings, "request_reason_required", True)
    for data in ({"perm": "printer", "printer": "ghost", "reason": "r"}, {"perm": "printer", "printer": "", "reason": "r"},
                 {"perm": "printer", "printer": "bw", "reason": "r"}, {"perm": "zzz", "reason": "r"}, {"perm": "color", "reason": ""}):
        r = await web.post("/me/request/permission", data={**data, "csrf_token": web.ann_token})
        assert r.headers["location"] == "/me/request/permission", data
    with web.sm() as s:
        assert not s.scalars(select(QuotaRequest)).all()
    await web.post("/me/request/permission", data={"perm": "color", "reason": "r", "csrf_token": web.ann_token})
    with web.sm() as s:
        rid = s.scalars(select(QuotaRequest)).one().id
    await web.post(f"/me/requests/{rid}/cancel", data={"csrf_token": web.ann_token})
    with web.sm() as s:
        assert s.get(QuotaRequest, rid).status is RequestStatus.canceled


async def test_ordinary_users_can_not_decide_grant_or_revoke_permissions(web):
    await web.post("/me/request/permission", data={"perm": "color", "reason": "r", "csrf_token": web.ann_token})
    with web.sm() as s:
        rid = s.scalars(select(QuotaRequest)).one().id
    assert (await web.post(f"/admin/requests/{rid}/approve", data={"csrf_token": web.ann_token})).status_code == 403
    assert (await web.post(f"/admin/requests/{rid}/reject", data={"csrf_token": web.ann_token})).status_code == 403
    assert (await web.post("/admin/grants", data={"username": "ann", "kind": "permission", "perm": "color", "csrf_token": web.ann_token})).status_code == 403
    assert (await web.post("/admin/permission-grants/1/revoke", data={"csrf_token": web.ann_token})).status_code == 403
    with web.sm() as s:
        assert not s.scalars(select(PermissionGrant)).all() and s.get(QuotaRequest, rid).status is RequestStatus.pending


async def test_direct_grant_form_permission_and_quota_revoke_and_notices(web):
    base = {"username": "ann", "csrf_token": web.boss_token, "amount": "", "metric": "jobs", "scope": "temporary"}
    r = await web.admin.post("/admin/grants", data={**base, "kind": "permission", "perm": "color", "duration": "permanent", "note": "gift"})
    assert r.headers["location"] == "/admin/requests"
    with web.sm() as s:
        g = s.scalars(select(PermissionGrant)).one()
        assert (g.perm, g.valid_until, g.note) == ("color", None, "gift")
    assert notices_of(web.sm, web.ann, "grant_given") == [{"what": "permission", "perm": "color", "printer": None, "until": None, "note": "gift"}]
    assert "An administrator gave you a permission: colour printing, with no end date" in (await web.get("/me/notices")).text
    with web.sm() as s:
        from spoolsteward.db.models import Metric, Period
        from spoolsteward.policy.rules import set_limit
        set_limit(s, SubjectType.user, web.ann, Metric.jobs, 5, Period.weekly)
        s.commit()
    # a form without JavaScript sends every field: only those of the chosen kind count
    await web.admin.post("/admin/grants", data={**base, "kind": "quota", "amount": "4", "perm": "hours", "printer": "zzz", "duration": "until", "until": "x"})
    with web.sm() as s:
        assert len(s.scalars(select(PermissionGrant)).all()) == 1 and s.scalars(select(QuotaGrant)).one().amount == 4
    html = (await web.admin.get("/admin/requests")).text
    assert "+ colour printing" in html and "+4 jobs" in html
    await web.admin.post(f"/admin/permission-grants/{g.id}/revoke", data={"csrf_token": web.boss_token})
    assert notices_of(web.sm, web.ann, "grant_revoked") == [{"what": "permission", "perm": "color", "printer": None}]
    assert "withdrew the permission you had been given: colour printing" in (await web.get("/me/notices")).text
    with web.sm() as s:
        assert not permissions.effective(s, s.get(User, web.ann), datetime.now(UTC)).color
    await web.admin.post(f"/admin/permission-grants/{g.id}/revoke", data={"csrf_token": web.boss_token})   # again: harmless
    assert len(notices_of(web.sm, web.ann, "grant_revoked")) == 1


async def test_direct_grant_form_refuses_bad_values_and_an_admin_is_not_notified_about_themselves(web):
    base = {"username": "ann", "kind": "permission", "csrf_token": web.boss_token}
    for data in ({"perm": "zzz"}, {"perm": "printer", "printer": "no good"}, {"perm": "color", "duration": "until", "until": ""},
                 {"perm": "color", "duration": "until", "until": "2001-01-01"}, {"kind": "other", "perm": "color"}, {"username": "nobody", "perm": "color"}):
        await web.admin.post("/admin/grants", data={**base, **data})
    with web.sm() as s:
        assert not s.scalars(select(PermissionGrant)).all()
    with web.sm() as s:                                                     # the admin lacks colour: it can be granted
        permissions.set_rule(s, SubjectType.user, web.boss, color=False)
        s.commit()
    await web.admin.post("/admin/grants", data={**base, "username": "boss", "perm": "color"})
    with web.sm() as s:
        assert s.scalars(select(PermissionGrant)).one().user_id == web.boss
    assert notices_of(web.sm, web.boss, "grant_given") == []


async def test_ended_permission_grants_show_in_the_history_and_the_purge_button_covers_them(web):
    now = datetime.now(UTC)
    with web.sm() as s:
        s.add_all([PermissionGrant(user_id=web.ann, perm="color", valid_from=now - timedelta(days=9), valid_until=now - timedelta(days=1)),
                   PermissionGrant(user_id=web.ann, perm="one_sided", valid_from=now)])
        s.commit()
    html = (await web.admin.get("/admin/requests")).text
    assert "Delete ended grants" in html and "ended" in html
    await web.admin.post("/admin/grants/purge", data={"csrf_token": web.boss_token})
    with web.sm() as s:
        assert [g.perm for g in s.scalars(select(PermissionGrant))] == ["one_sided"]
    assert "Delete ended grants" not in (await web.admin.get("/admin/requests")).text


# ---------------------------------------------------------------- the database

def test_a_permission_request_can_be_stored_after_the_migrations(tmp_path):
    """The kind column is plain text (the enum has no CHECK constraint), so the new kind needs no change of the old table."""
    eng = create_engine(f"sqlite:///{tmp_path}/m.db")
    upgrade(eng)
    with eng.connect() as conn:
        assert "CHECK" not in conn.execute(text("select sql from sqlite_master where name = 'quota_request'")).scalar().upper()
    with sessionmaker(eng)() as s:
        s.add(User(provider="local", username="u"))
        s.commit()
        uid = s.scalars(select(User)).one().id
        now = datetime.now(UTC)
        req = QuotaRequest(user_id=uid, kind=RequestKind.permission, params_json={"perm": "color", "printer": None}, reason="r",
                           status=RequestStatus.pending, created_at=now, expires_at=now + timedelta(days=3))
        s.add(req)
        s.commit()
        s.add(PermissionGrant(user_id=uid, perm="color", valid_from=now, request_id=req.id))
        s.commit()
        assert s.get(QuotaRequest, req.id).kind is RequestKind.permission


async def test_a_permission_already_asked_for_is_not_offered_again(portal):
    """While the answer is awaited, /me says "request waiting" instead of "Ask", and the form does not offer it again."""
    from spoolsteward.db.models import SubjectType
    from spoolsteward.policy import grants as grants_mod, permissions as perm_mod
    from tests.conftest import make_user, portal_login
    uid = make_user(portal.sm, "ann", password="correct horse battery")
    with portal.sm() as s:
        perm_mod.set_rule(s, SubjectType.user, uid, color=False, one_sided=False)
        s.commit()
        from spoolsteward.db.models import User
        grants_mod.create_permission_request(s, s.get(User, uid), "color", None, "posters", datetime.now(UTC))
    await portal_login(portal, "ann", "correct horse battery")
    me = (await portal.get("/me")).text
    assert "request waiting" in me and "/me/request/permission?perm=color" not in me
    assert "/me/request/permission?perm=one_sided" in me
    form = (await portal.get("/me/request/permission")).text
    assert 'value="color"' not in form and 'value="one_sided"' in form



async def test_a_permission_the_user_already_has_can_not_be_granted_and_the_form_knows_it(web):
    """boss has every permission: the direct grant is refused, and the page tells the form there is nothing to offer."""
    r = await web.admin.post("/admin/grants", data={"username": "boss", "kind": "permission", "perm": "color", "csrf_token": web.boss_token})
    assert r.status_code == 303
    with web.sm() as s:
        assert not s.scalars(select(PermissionGrant)).all()
    assert "already has every permission" in (await web.admin.get("/admin/requests")).text
    assert (await web.admin.get("/admin/requests/lacks?username=boss")).json() == {"perms": [], "printers": [], "metrics": []}
    with web.sm() as s:
        permissions.set_rule(s, SubjectType.user, web.boss, one_sided=False)
        s.commit()
    assert (await web.admin.get("/admin/requests/lacks?username=boss")).json() == {"perms": ["one_sided"], "printers": [], "metrics": []}
    assert (await web.admin.get("/admin/requests/lacks?username=nobody")).status_code == 404
    assert (await web.get("/admin/requests/lacks?username=boss")).status_code in (302, 303, 401, 403)   # administrators only


def test_deleting_a_user_removes_their_permissions_and_grants(web):
    from spoolsteward.auth.users import delete_user
    from spoolsteward.db.models import PermissionRule
    now = datetime.now(UTC)
    with web.sm() as s:
        permissions.set_rule(s, SubjectType.user, web.ann, color=False)
        s.add_all([PermissionGrant(user_id=web.ann, perm="color", valid_from=now),
                   PermissionGrant(user_id=web.boss, perm="hours", valid_from=now, granted_by=web.ann, revoked_by=web.ann)])
        s.commit()
        delete_user(s, s.get(User, web.ann))
        s.commit()
        assert s.query(PermissionRule).count() == 0
        [left] = s.scalars(select(PermissionGrant)).all()
        assert left.user_id == web.boss and left.granted_by is None and left.revoked_by is None


def test_renaming_a_printer_moves_the_grants_and_the_waiting_requests_for_it(web):
    now = datetime.now(UTC)
    with web.sm() as s:
        s.add(PermissionGrant(user_id=web.ann, perm="printer", printer="color", valid_from=now))
        s.add(QuotaRequest(user_id=web.ann, kind=RequestKind.permission, params_json={"perm": "printer", "printer": "color"},
                           reason="x", status=RequestStatus.pending, created_at=now, expires_at=now + timedelta(days=3)))
        s.commit()
        permissions.rename_printer(s, "Color", "Office")
        s.commit()
        assert s.scalars(select(PermissionGrant)).one().printer == "office"
        assert s.scalars(select(QuotaRequest)).one().params_json["printer"] == "office"
