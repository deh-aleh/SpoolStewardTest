"""Pins what POST /admin/users/{id} does branch by branch (messages, redirects, audit rows, notices, order of the checks),
so that the function can be split into helpers without changing anything."""

import html
import re

import pytest
from sqlalchemy import select

from app.auth.groups import ADMIN_GROUP, ensure_system_groups
from app.config import settings
from app.db.models import AdminAudit, Group, Notice, SubjectType, User
from app.policy import permissions
from tests.conftest import make_user, user_row

pytestmark = pytest.mark.usefixtures("english")

GOOD = "correct horse battery"


@pytest.fixture
async def adm(admin):
    ensure_system_groups(admin.sm)
    with admin.sm() as s:
        s.add(Group(name="staff"))
        s.commit()
    admin.token = re.search(r'name="csrf_token" value="([^"]+)"', (await admin.get("/admin/users")).text).group(1)
    return admin


def gid(cl, name):
    with cl.sm() as s:
        return s.scalars(select(Group).where(Group.name == name)).one().id


async def post(cl, uid, **data):
    """(status, location, flashes shown on the next page)"""
    r = await cl.post(f"/admin/users/{uid}", data={"csrf_token": cl.token, **data})
    if r.status_code != 303:
        return r.status_code, None, r
    page = (await cl.get(r.headers["location"])).text
    return r.status_code, r.headers["location"], [html.unescape(m) for m in re.findall(r'class="flash (?:ok|err)">([^<]*)<', page)]


def audits(cl):
    with cl.sm() as s:
        return [(a.action, a.target_id, a.detail_json) for a in s.scalars(select(AdminAudit).where(AdminAudit.action != "login").order_by(AdminAudit.id))]


def notices_of(cl, uid):
    with cl.sm() as s:
        return [(n.kind, n.detail_json) for n in s.scalars(select(Notice).where(Notice.user_id == uid).order_by(Notice.id))]


async def test_unknown_user_is_a_404(adm):
    r = await adm.post("/admin/users/9999", data={"csrf_token": adm.token, "enabled": "1"})
    assert r.status_code == 404


@pytest.mark.parametrize("extra, msg", [
    (dict(jobs_limit="x"), "invalid limit 'x'"),
    (dict(pages_limit="-1"), "invalid limit '-1'"),
    (dict(period="yearly-ish"), "'yearly-ish' is not a valid Period"),
    (dict(perm_form="1", perm_color="maybe"), "Invalid permission value"),
])
async def test_form_errors_go_back_to_the_edit_page(adm, extra, msg):
    uid = make_user(adm.sm, "ann", password=GOOD, group="staff")
    status, loc, fl = await post(adm, uid, enabled="1", **extra)
    assert (status, loc) == (303, f"/admin/users/{uid}/edit") and fl[-1] == msg and audits(adm) == []


async def test_emergency_admin_ignores_a_bad_limit_but_not_a_bad_period(adm):
    uid = make_user(adm.sm, settings.bootstrap_admin_username, password=GOOD, admin=True)
    status, loc, fl = await post(adm, uid, enabled="1", is_admin="1", jobs_limit="zzz", pages_limit="zzz")
    assert (status, loc, fl) == (303, "/admin/users", ["Saved administrator"])
    status, loc, fl = await post(adm, uid, enabled="1", is_admin="1", period="nope")
    assert loc == f"/admin/users/{uid}/edit" and "not a valid Period" in fl[-1]


async def test_emergency_admin_posted_permissions_are_not_even_validated(adm):
    uid = make_user(adm.sm, settings.bootstrap_admin_username, password=GOOD, admin=True)
    status, loc, fl = await post(adm, uid, enabled="1", is_admin="1", perm_form="1", perm_color="maybe")
    assert (loc, fl) == ("/admin/users", ["Saved administrator"])


async def test_a_post_without_perm_form_leaves_the_permissions_alone(adm):
    uid = make_user(adm.sm, "ann", password=GOOD, group="staff")
    with adm.sm() as s:
        permissions.set_rule(s, SubjectType.user, uid, color=False)
        s.commit()
    await post(adm, uid, enabled="1", group_id=str(gid(adm, "staff")), perm_color="1")
    with adm.sm() as s:
        assert permissions.rule_fields(permissions.get_rule(s, SubjectType.user, uid))["color"] is False
    assert "permissions" not in audits(adm)[-1][2]


async def test_unknown_group_unknown_provider(adm):
    uid = make_user(adm.sm, "ann", password=GOOD, group="staff")
    assert (await post(adm, uid, enabled="1", group_id="9999"))[1:] == (f"/admin/users/{uid}/edit", ["Unknown group"])
    assert (await post(adm, uid, enabled="1", provider="ldap"))[1:] == (f"/admin/users/{uid}/edit", ["Unknown or unavailable login method"])
    assert audits(adm) == []


async def test_the_form_order_group_is_checked_before_provider(adm):
    uid = make_user(adm.sm, "ann", password=GOOD, group="staff")
    assert (await post(adm, uid, enabled="1", group_id="9999", provider="ldap"))[2] == ["Unknown group"]


async def test_an_account_whose_provider_is_gone_may_keep_it(adm):
    uid = make_user(adm.sm, "old", password=GOOD, provider="ldap")
    status, loc, fl = await post(adm, uid, enabled="1")        # provider "" means "as it is"
    assert (loc, fl) == ("/admin/users", ["Saved old"])
    assert user_row(adm, "old").provider == "ldap"


async def test_protection_of_the_last_admin_yourself_and_the_emergency_admin(adm):
    boss = user_row(adm, "boss").id
    em = make_user(adm.sm, settings.bootstrap_admin_username, password=GOOD, admin=True)
    edit = lambda i: f"/admin/users/{i}/edit"  # noqa: E731
    assert (await post(adm, boss, is_admin="1"))[1:] == (edit(boss), ["You cannot disable or demote yourself"])
    assert (await post(adm, boss, enabled="1"))[1:] == (edit(boss), ["You cannot disable or demote yourself"])
    assert (await post(adm, em, enabled="1"))[1:] == (
        edit(em), ["administrator is the emergency administrator: it can not be disabled or demoted"])
    assert (await post(adm, em, is_admin="1"))[1:] == (
        edit(em), ["administrator is the emergency administrator: it can not be disabled or demoted"])
    assert audits(adm) == []


async def test_the_last_administrator_is_protected_from_another_caller(adm):
    # the caller (boss) is another administrator, the target is the only other enabled one besides boss: boss disabled below
    other = make_user(adm.sm, "other", password=GOOD, admin=True)
    status, loc, fl = await post(adm, other, is_admin="1")                       # disabling: allowed, boss remains
    assert (loc, fl) == ("/admin/users", ["Saved other"]) and not user_row(adm, "other").enabled
    status, loc, fl = await post(adm, other)                                     # demote and disable a disabled admin
    assert fl == ["Saved other"] and not user_row(adm, "other").is_admin


async def test_the_name_reserved_for_the_emergency_admin(adm):
    uid = make_user(adm.sm, settings.bootstrap_admin_username, provider="radius")
    status, loc, fl = await post(adm, uid, enabled="1", provider="local")
    assert (loc, fl) == (f"/admin/users/{uid}/edit", ["The name administrator is reserved for the emergency administrator"])
    assert user_row(adm, "administrator").provider == "radius"


async def test_the_emergency_admin_must_stay_local(adm):
    uid = make_user(adm.sm, settings.bootstrap_admin_username, password=GOOD, admin=True)
    status, loc, fl = await post(adm, uid, enabled="1", is_admin="1", provider="radius")
    assert (loc, fl) == (f"/admin/users/{uid}/edit", ["administrator is the emergency administrator: it must stay a local account"])
    assert user_row(adm, "administrator").provider == "local" and audits(adm) == []


async def test_the_checks_before_the_changes_run_in_this_order(adm):
    # protected-admin check comes before the reserved-name and the provider checks
    uid = make_user(adm.sm, settings.bootstrap_admin_username, password=GOOD, admin=True)
    assert (await post(adm, uid, provider="radius"))[2] == [
        "administrator is the emergency administrator: it can not be disabled or demoted"]


async def test_group_rules_for_the_administrator_group(adm):
    ag = gid(adm, ADMIN_GROUP)
    staff = gid(adm, "staff")
    ann = make_user(adm.sm, "ann", password=GOOD, group="staff")
    status, loc, fl = await post(adm, ann, enabled="1", group_id=str(ag))
    assert (loc, fl) == (f"/admin/users/{ann}/edit", ['Group "administrator" is only for administrators'])
    other = make_user(adm.sm, "other", password=GOOD, admin=True)
    with adm.sm() as s:
        s.get(User, other).group_id = ag
        s.commit()
    status, loc, fl = await post(adm, other, enabled="1", is_admin="1", group_id=str(staff))
    assert (loc, fl) == (f"/admin/users/{other}/edit", ['Administrators stay in "administrator": remove the admin right first'])
    status, loc, fl = await post(adm, other, enabled="1", is_admin="1", group_id="0")
    assert (loc, fl) == (f"/admin/users/{other}/edit", ['Administrators stay in "administrator": remove the admin right first'])
    assert user_row(adm, "ann").group_id == staff and user_row(adm, "other").group_id == ag


async def test_changing_the_group_is_audited_and_the_person_is_told(adm):
    other = gid(adm, "staff")
    with adm.sm() as s:
        s.add(Group(name="lab"))
        s.commit()
    lab = gid(adm, "lab")
    ann = make_user(adm.sm, "ann", password=GOOD, group="staff")
    status, loc, fl = await post(adm, ann, enabled="1", group_id=str(lab), jobs_limit="5", pages_limit="9", period="monthly")
    assert (loc, fl) == ("/admin/users", ["Saved ann"])
    act, tid, detail = audits(adm)[-1]
    assert (act, tid) == ("user.update", ann)
    assert detail == {"username": "ann", "limits": {"jobs": 5, "pages": 9, "period": "monthly"}, "group_id": [other, lab]}
    assert notices_of(adm, ann) == [("group_changed", {"old": "staff", "new": "lab"})]
    assert user_row(adm, "ann").group_id == lab
    # no group ("—"): the group becomes none
    await post(adm, ann, enabled="1", group_id="0")
    assert audits(adm)[-1][2]["group_id"] == [lab, None] and user_row(adm, "ann").group_id is None
    assert notices_of(adm, ann)[-1] == ("group_changed", {"old": "lab", "new": ""})


async def test_promotion_demotion_and_disabling(adm):
    ag, staff = gid(adm, ADMIN_GROUP), gid(adm, "staff")
    ann = make_user(adm.sm, "ann", password=GOOD, group="staff")
    status, loc, fl = await post(adm, ann, enabled="1", is_admin="1", group_id=str(staff))
    u = user_row(adm, "ann")
    assert (loc, fl) == ("/admin/users", ["Saved ann"]) and u.is_admin and u.group_id == ag
    assert audits(adm)[-1][2] == {"username": "ann", "limits": {"jobs": None, "pages": None, "period": "weekly"},
                                  "is_admin": True, "group_id": [staff, ag]}
    assert notices_of(adm, ann) == [("admin_on", {})]
    # demoted, a group chosen: the chosen one wins; "enabled" off in the same save
    await post(adm, ann, group_id=str(staff))
    u = user_row(adm, "ann")
    assert not u.is_admin and not u.enabled and u.group_id == staff
    assert audits(adm)[-1][2] == {"username": "ann", "limits": {"jobs": None, "pages": None, "period": "weekly"},
                                  "enabled": False, "is_admin": False, "group_id": [ag, staff]}
    assert notices_of(adm, ann)[-1] == ("admin_off", {"group": "staff"})
    # demoted with the administrator group chosen: ignored, goes to the default group
    await post(adm, ann, enabled="1", is_admin="1", group_id=str(staff))
    await post(adm, ann, enabled="1", group_id=str(ag))
    u = user_row(adm, "ann")
    assert not u.is_admin and u.group_id == gid(adm, settings.default_group)


async def test_nothing_changed_means_no_notice_and_a_plain_audit_row(adm):
    ann = make_user(adm.sm, "ann", password=GOOD, group="staff")
    status, loc, fl = await post(adm, ann, enabled="1", group_id=str(gid(adm, "staff")))
    assert fl == ["Saved ann"] and notices_of(adm, ann) == []
    assert audits(adm)[-1] == ("user.update", ann, {"username": "ann", "limits": {"jobs": None, "pages": None, "period": "weekly"}})


async def test_the_emergency_admin_is_saved_without_limits_or_group(adm):
    uid = make_user(adm.sm, settings.bootstrap_admin_username, password=GOOD, admin=True)
    await post(adm, uid, enabled="1", is_admin="1", group_id=str(gid(adm, "staff")), jobs_limit="3", perm_form="1", perm_color="0")
    assert audits(adm)[-1] == ("user.update", uid, {"username": "administrator"})
    assert user_row(adm, "administrator").group_id is None


async def test_permission_changes_are_audited_and_the_person_is_told(adm):
    ann = make_user(adm.sm, "ann", password=GOOD, group="staff")
    await post(adm, ann, enabled="1", group_id=str(gid(adm, "staff")), perm_form="1", perm_color="0", perm_printers="list", perm_printer=["bw", "Color"])
    detail = audits(adm)[-1][2]
    assert detail["permissions"] == {"color": [None, False], "printers_mode": [None, "list"], "printers": [None, ["bw", "color"]]}
    assert ("permissions_changed", {}) in notices_of(adm, ann)
    n = len(audits(adm))
    await post(adm, ann, enabled="1", group_id=str(gid(adm, "staff")), perm_form="1", perm_color="0", perm_printers="list", perm_printer=["bw", "Color"])
    assert "permissions" not in audits(adm)[n][2] and [k for k, _ in notices_of(adm, ann)].count("permissions_changed") == 1


async def test_nobody_is_told_about_their_own_changes(adm):
    boss = user_row(adm, "boss").id
    await post(adm, boss, enabled="1", is_admin="1", group_id=str(gid(adm, ADMIN_GROUP)), perm_form="1", perm_color="0")
    assert notices_of(adm, boss) == []
    assert "permissions" in audits(adm)[-1][2]


async def test_origin_switch_to_local_shows_a_temporary_password_once(adm):
    uid = make_user(adm.sm, "rad", provider="radius", group="staff")
    before = user_row(adm, "rad").password_changed_at
    r = await adm.post(f"/admin/users/{uid}", data={"csrf_token": adm.token, "enabled": "1", "provider": "local", "group_id": str(gid(adm, "staff"))})
    assert r.status_code == 200
    text = html.unescape(r.text)
    assert "rad is now a local account" in text and "Temporary password" in text and "/admin/users" in text
    assert "The user must choose their own at the next login. Their print keys keep working." in text
    u = user_row(adm, "rad")
    assert u.provider == "local" and u.password_hash and u.must_change_password and u.password_changed_at != before
    assert audits(adm)[-1][2]["provider"] == ["radius", "local"]


async def test_origin_switch_to_radius_drops_the_password(adm):
    uid = make_user(adm.sm, "ann", password=GOOD, group="staff")
    status, loc, fl = await post(adm, uid, enabled="1", provider="radius", group_id=str(gid(adm, "staff")))
    u = user_row(adm, "ann")
    assert (loc, fl) == ("/admin/users", ["Saved ann"])
    assert (u.provider, u.password_hash, u.must_change_password) == ("radius", None, False) and u.password_changed_at is not None
    assert audits(adm)[-1][2]["provider"] == ["local", "radius"]
