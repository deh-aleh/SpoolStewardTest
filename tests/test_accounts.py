"""Phase 1: local accounts, the first-run administrator, user/group administration, optional RADIUS."""

import re
from datetime import UTC

import pytest
from sqlalchemy import select

from spoolsteward.auth import bootstrap
from spoolsteward.auth.groups import ADMIN_GROUP, ensure_system_groups, set_admin
from spoolsteward.auth.passwords import hash_password, validate_new_password, verify_password
from spoolsteward.config import settings
from spoolsteward.db.models import (AdminAudit, Group, JobLog, JobState, Metric, Period, PolicyRule, PrintKey,
                           QuotaGrant, SubjectType, User)
from spoolsteward.policy.rules import set_limit
from tests.conftest import csrf_of_page, make_user, portal_login, user_row

ORCH = "administrator"
GOOD = "correct horse battery"


def audit_actions(cl):
    with cl.sm() as s:
        return [a.action for a in s.scalars(select(AdminAudit).order_by(AdminAudit.id))]


# ---------------------------------------------------------------- passwords

def test_password_policy():
    assert validate_new_password("short", "x")
    assert validate_new_password("a" * 12, "x") is not None                  # too repetitive
    assert validate_new_password("MyUserName1234", "myusername1234") is not None
    assert validate_new_password("x" * 300, "x") is not None
    assert validate_new_password(GOOD, "bob") is None


def test_hash_is_argon2_and_verifies_in_constant_shape():
    h = hash_password(GOOD)
    assert h.startswith("$argon2id$") and GOOD not in h
    assert verify_password(h, GOOD) and not verify_password(h, GOOD + "x")
    assert not verify_password(None, GOOD)               # unknown account: false, but did the same work
    assert not verify_password("not a hash", GOOD)       # corrupted row never raises


# ---------------------------------------------------------------- first-run administrator

async def test_bootstrap_creates_passwordless_orchestrator_with_a_code(portal):
    code = bootstrap.ensure_orchestrator(portal.sm)
    assert code and len(code) >= 16
    u = user_row(portal, ORCH)
    assert (u.provider, u.is_admin, u.enabled, u.password_hash) == ("local", True, True, None)
    assert bootstrap.ensure_orchestrator(portal.sm) not in (None, code)   # a restart prints a fresh code
    with portal.sm() as s:
        u = s.scalars(select(User).where(User.username == ORCH)).one()
        u.password_hash = hash_password(GOOD)
        s.commit()
    assert bootstrap.ensure_orchestrator(portal.sm) is None               # nothing pending once it has a password


async def test_bootstrap_can_be_switched_off(portal, monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_admin", False)
    assert bootstrap.ensure_orchestrator(portal.sm) is None and user_row(portal, ORCH) is None


async def test_setup_page_exists_only_while_pending(portal):
    assert (await portal.get("/setup")).status_code == 404
    portal.app.state.setup_code = "the-code"
    assert (await portal.get("/setup")).status_code == 200
    assert "/setup" in (await portal.get("/login")).text       # the login page points to it


async def test_setup_flow_sets_the_password_once(portal):
    bootstrap_code = bootstrap.ensure_orchestrator(portal.sm)
    portal.app.state.setup_code = bootstrap_code
    bad = await portal.post("/setup", data={"code": "wrong", "password": GOOD, "confirm": GOOD})
    assert bad.status_code == 303 and user_row(portal, ORCH).password_hash is None      # wrong code
    r = await portal.post("/setup", data={"code": bootstrap_code, "password": GOOD, "confirm": GOOD + "x"})
    assert user_row(portal, ORCH).password_hash is None                                   # passwords differ
    r = await portal.post("/setup", data={"code": bootstrap_code, "password": "short", "confirm": "short"})
    assert user_row(portal, ORCH).password_hash is None                                   # too weak
    r = await portal.post("/setup", data={"code": bootstrap_code, "password": GOOD, "confirm": GOOD})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert verify_password(user_row(portal, ORCH).password_hash, GOOD)
    assert "admin.setup" in audit_actions(portal)
    # the code is burned: neither page nor POST works any more
    assert (await portal.get("/setup")).status_code == 404
    again = await portal.post("/setup", data={"code": bootstrap_code, "password": GOOD + "!", "confirm": GOOD + "!"})
    assert again.status_code == 404 and verify_password(user_row(portal, ORCH).password_hash, GOOD)
    assert (await portal_login(portal, ORCH, GOOD)).headers["location"] == "/admin/"


async def test_setup_code_guessing_is_throttled(portal):
    portal.app.state.setup_code = "right-code-1234567890"
    for i in range(5):
        await portal.post("/setup", data={"code": f"guess{i}", "password": GOOD, "confirm": GOOD})
    r = await portal.post("/setup", data={"code": "right-code-1234567890", "password": GOOD, "confirm": GOOD})
    assert r.status_code == 303 and r.headers["location"] == "/setup"     # locked out, even with the right code
    assert portal.app.state.setup_code == "right-code-1234567890"


# ---------------------------------------------------------------- logging in

async def test_local_login_and_where_each_kind_of_user_lands(portal):
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    make_user(portal.sm, "ann", password=GOOD)
    assert (await portal_login(portal, "ann", GOOD + "x")).headers["location"] == "/login"
    assert (await portal_login(portal, "ann", GOOD)).headers["location"] == "/me"
    assert (await portal.get("/admin/jobs")).status_code == 403
    await portal.post("/logout", data={"csrf_token": await csrf_of_page(portal)})
    assert (await portal_login(portal, "BOSS ", GOOD)).headers["location"] == "/admin/"
    assert (await portal.get("/admin/jobs")).status_code == 200
    assert "login" in audit_actions(portal)                                          # admin logins are recorded


async def test_disabled_and_unknown_local_users_cannot_log_in(portal):
    make_user(portal.sm, "off", password=GOOD, enabled=False)
    assert (await portal_login(portal, "off", GOOD)).headers["location"] == "/login"
    assert (await portal_login(portal, "ghost", GOOD)).headers["location"] == "/login"


async def test_radius_cannot_open_a_local_account(portal):
    """The provider is decided by the row in the database, never by who says yes."""
    make_user(portal.sm, "rad", password=GOOD)                    # LOCAL account that happens to share a RADIUS name
    assert (await portal_login(portal, "rad", "radpw")).headers["location"] == "/login"   # RADIUS would say yes
    assert (await portal_login(portal, "rad", GOOD)).headers["location"] == "/me"                # the local password works


async def test_radius_user_is_created_at_first_login_and_name_collisions_are_refused(portal):
    assert user_row(portal, "rad") is None
    assert (await portal_login(portal, "rad", "radpw")).headers["location"] == "/me"
    u = user_row(portal, "rad")
    assert u.provider == "radius" and u.password_hash is None
    # a local user can not take a name that is already somebody else's
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    await portal.post("/logout", data={"csrf_token": await csrf_of_page(portal)})
    await portal_login(portal, "boss", GOOD)
    token = await csrf_of_page(portal, "/admin/users")
    r = await portal.post("/admin/users/local", data={"username": "rad", "csrf_token": token})
    assert r.status_code == 303
    assert user_row(portal, "rad").provider == "radius"


async def test_without_radius_only_local_accounts_exist(portal):
    portal.app.state.auth = None
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    assert (await portal_login(portal, "rad", "radpw")).headers["location"] == "/login"
    assert user_row(portal, "rad") is None
    await portal_login(portal, "boss", GOOD)
    token = await csrf_of_page(portal, "/admin/users")
    html = (await portal.get("/admin/users")).text
    assert "Pre-register a RADIUS user" not in html and "New local user" in html
    await portal.post("/admin/users", data={"username": "x", "csrf_token": token})
    assert user_row(portal, "x") is None                                              # no provider to pre-register for


# ---------------------------------------------------------------- forced password change

async def test_temporary_password_forces_a_change_and_ends_other_sessions(portal):
    make_user(portal.sm, "tmp", password="Temp-password-1", must_change=True)
    first = await portal_login(portal, "tmp", "Temp-password-1")
    assert first.headers["location"] == "/me/password"
    assert (await portal.get("/me")).status_code == 303 and (await portal.get("/me")).headers["location"] == "/me/password"
    token = await csrf_of_page(portal, "/me/password")
    for new, current in (("short", "Temp-password-1"), ("Temp-password-1", "Temp-password-1"), ("a-fine-new-pass1", "wrong")):
        await portal.post("/me/password", data={"current": current, "new": new, "confirm": new, "csrf_token": token})
        assert user_row(portal, "tmp").must_change_password                              # nothing accepted
    # a second browser logged in with the same temporary password
    import httpx
    other = httpx.AsyncClient(transport=httpx.ASGITransport(app=portal.app, client=("10.0.0.8", 1)), base_url="https://gw.test")
    async with other:
        await other.post("/login", data={"username": "tmp", "password": "Temp-password-1"})
        r = await portal.post("/me/password", data={"current": "Temp-password-1", "new": "a-fine-new-pass1",
                                                    "confirm": "a-fine-new-pass1", "csrf_token": token})
        assert r.status_code == 303 and r.headers["location"] == "/me"
        assert not user_row(portal, "tmp").must_change_password
        assert (await portal.get("/me")).status_code == 200                              # this session survives
        assert (await other.get("/me")).status_code == 303                               # the other one is ended


async def test_the_forced_password_page_is_the_centred_box_with_show_buttons(portal):
    make_user(portal.sm, "tmp", password="Temp-password-1", must_change=True)
    await portal_login(portal, "tmp", "Temp-password-1")
    html = (await portal.get("/me/password")).text
    assert 'class="authmain"' in html and "authbox" in html and html.count('class="pwtoggle"') == 3


async def test_radius_users_have_no_password_page(portal):
    await portal_login(portal, "rad", "radpw")
    r = await portal.get("/me/password")
    assert r.status_code == 303 and r.headers["location"] == "/me"


# ---------------------------------------------------------------- administering users and groups

@pytest.fixture
async def boss(portal):
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    make_user(portal.sm, ORCH, password=GOOD, admin=True)
    await portal_login(portal, "boss", GOOD)
    portal.token = await csrf_of_page(portal, "/admin/users")
    return portal


async def test_admin_creates_a_local_user_whose_temporary_password_is_shown_once(boss):
    r = await boss.post("/admin/users/local", data={"username": " NewGuy ", "csrf_token": boss.token})
    assert r.status_code == 200 and "Temporary password" in r.text
    temp = re.search(r'class="secret">([^<]+)<', r.text).group(1)
    u = user_row(boss, "newguy")
    assert u.provider == "local" and u.must_change_password and verify_password(u.password_hash, temp)
    assert temp not in (await boss.get("/admin/users")).text                          # never shown again
    assert "user.create" in audit_actions(boss)
    assert "no-store" in r.headers["cache-control"]


async def test_admin_user_creation_validation(boss):
    for name in ("", "a b", "-x", "x" * 80, "<script>"):
        await boss.post("/admin/users/local", data={"username": name, "csrf_token": boss.token})
    await boss.post("/admin/users/local", data={"username": "weak", "password": "short", "csrf_token": boss.token})
    await boss.post("/admin/users/local", data={"username": "grp", "group_id": "999", "csrf_token": boss.token})
    with boss.sm() as s:
        assert sorted(u.username for u in s.scalars(select(User))) == sorted(["boss", ORCH])
    await boss.post("/admin/users/local", data={"username": "adm", "password": GOOD, "is_admin": "1",
                                                    "csrf_token": boss.token})
    assert user_row(boss, "adm").is_admin and verify_password(user_row(boss, "adm").password_hash, GOOD)


async def test_admin_resets_a_local_password_and_not_a_radius_one(boss):
    make_user(boss.sm, "ann", password=GOOD)
    make_user(boss.sm, "radu", provider="radius")
    uid = user_row(boss, "ann").id
    r = await boss.post(f"/admin/users/{uid}/password", data={"csrf_token": boss.token})
    temp = re.search(r'class="secret">([^<]+)<', r.text).group(1)
    u = user_row(boss, "ann")
    assert u.must_change_password and verify_password(u.password_hash, temp) and not verify_password(u.password_hash, GOOD)
    await boss.post(f"/admin/users/{user_row(boss, 'radu').id}/password", data={"csrf_token": boss.token})
    assert user_row(boss, "radu").password_hash is None
    assert "user.reset_password" in audit_actions(boss)


async def test_the_emergency_admin_can_not_be_disabled_or_demoted(boss):
    orch = user_row(boss, ORCH).id
    for form in ({"is_admin": "1"}, {"enabled": "1"}, {}):                           # disable / demote / both
        await boss.post(f"/admin/users/{orch}", data={**form, "csrf_token": boss.token})
        u = user_row(boss, ORCH)
        assert u.enabled and u.is_admin


async def test_other_admins_can_be_demoted_but_never_yourself(boss):
    make_user(boss.sm, "other", password=GOOD, admin=True)
    oid, bid = user_row(boss, "other").id, user_row(boss, "boss").id
    await boss.post(f"/admin/users/{oid}", data={"enabled": "1", "csrf_token": boss.token})
    assert not user_row(boss, "other").is_admin
    await boss.post(f"/admin/users/{bid}", data={"enabled": "1", "csrf_token": boss.token})   # demote myself
    await boss.post(f"/admin/users/{bid}", data={"is_admin": "1", "csrf_token": boss.token})  # disable myself
    assert user_row(boss, "boss").is_admin and user_row(boss, "boss").enabled


async def test_demoting_an_admin_and_choosing_a_group_in_one_save_keeps_the_chosen_group(boss):
    with boss.sm() as s:
        s.add(Group(name="staff"))
        s.commit()
        staff = s.scalars(select(Group).where(Group.name == "staff")).first().id
        admins = s.scalars(select(Group).where(Group.name == "administrator")).first()
        admins_id = admins.id if admins else None
    make_user(boss.sm, "other", password=GOOD, admin=True)
    oid = user_row(boss, "other").id
    await boss.post(f"/admin/users/{oid}", data={"enabled": "1", "group_id": str(staff), "csrf_token": boss.token})
    u = user_row(boss, "other")
    assert not u.is_admin and u.group_id == staff
    # promoted again: always "administrator", whatever the form says
    await boss.post(f"/admin/users/{oid}", data={"enabled": "1", "is_admin": "1", "group_id": str(staff), "csrf_token": boss.token})
    u = user_row(boss, "other")
    assert u.is_admin and (admins_id is None or u.group_id == admins_id)
    # demoted with "—": back to the default group
    await boss.post(f"/admin/users/{oid}", data={"enabled": "1", "group_id": "0", "csrf_token": boss.token})
    with boss.sm() as s:
        u = s.scalars(select(User).where(User.username == "other")).first()
        assert not u.is_admin and s.get(Group, u.group_id).name == settings.default_group


def test_the_last_enabled_admin_is_protected_even_from_a_non_admin_caller(portal):
    """Defence in depth for races (two admins removing each other at once)."""
    from spoolsteward.admin.accounts import _protected_admin

    only = make_user(portal.sm, "only", password=GOOD, admin=True)
    nobody = make_user(portal.sm, "plain", password=GOOD)
    with portal.sm() as s:
        assert "last administrator" in _protected_admin(s, s.get(User, only), s.get(User, nobody))
        make_user(portal.sm, "second", password=GOOD, admin=True)
        assert _protected_admin(s, s.get(User, only), s.get(User, nobody)) is None
        assert "yourself" in _protected_admin(s, s.get(User, only), s.get(User, only))


async def test_group_and_user_changes_leave_an_admin_trail(boss):
    await boss.post("/admin/groups", data={"name": "staff", "csrf_token": boss.token})
    make_user(boss.sm, "ann", password=GOOD)
    uid = user_row(boss, "ann").id
    await boss.post(f"/admin/users/{uid}", data={"enabled": "1", "jobs_limit": "5", "csrf_token": boss.token})
    acts = audit_actions(boss)
    assert "group.create" in acts and "user.update" in acts
    with boss.sm() as s:
        a = s.scalars(select(AdminAudit).where(AdminAudit.action == "user.update")).one()
        assert a.actor_name == "boss" and a.detail_json["limits"]["jobs"] == 5


async def test_admin_pages_refuse_plain_users_and_csrf_is_required(boss):
    make_user(boss.sm, "ann", password=GOOD)
    user_row(boss, "ann").id
    r = await boss.post("/admin/users/local", data={"username": "nope"})
    assert r.status_code == 403 and user_row(boss, "nope") is None                    # no CSRF token




def test_a_log_line_tells_when_there_is_no_first_run_code(portal, caplog):
    import logging

    from sqlalchemy import select

    from spoolsteward.auth import bootstrap
    from spoolsteward.auth.passwords import hash_password
    from spoolsteward.db.models import User

    with caplog.at_level(logging.INFO, logger="spoolsteward.auth.bootstrap"):
        assert bootstrap.ensure_orchestrator(portal.sm) and "FIRST RUN" in caplog.text
        caplog.clear()
        with portal.sm() as s:
            s.scalars(select(User).where(User.username == "administrator")).one().password_hash = hash_password("a long password 123")
            s.commit()
        assert bootstrap.ensure_orchestrator(portal.sm) is None
        assert "already has a password" in caplog.text and "FIRST RUN" not in caplog.text


def group_name(cl, user_id):
    with cl.sm() as s:
        u = s.get(User, user_id)
        return s.get(Group, u.group_id).name if u.group_id else None


# ---------------------------------------------------------------- system groups

def test_startup_creates_default_and_administrator_and_files_everybody(portal):
    a = make_user(portal.sm, "adm", admin=True, password=GOOD)
    b = make_user(portal.sm, "usr", password=GOOD, group=ADMIN_GROUP)          # wrongly in the admin group
    ensure_system_groups(portal.sm)
    with portal.sm() as s:
        names = {g.name for g in s.scalars(select(Group))}
    assert {settings.default_group, ADMIN_GROUP} <= names
    assert group_name(portal, a) == ADMIN_GROUP and group_name(portal, b) == settings.default_group


async def test_promoting_and_demoting_moves_the_account_between_the_two_groups(admin):
    uid = make_user(admin.sm, "carla", password=GOOD, group="staff")
    tok = await csrf_of_page(admin, "/admin/users")
    r = await admin.post(f"/admin/users/{uid}", data={"csrf_token": tok, "enabled": "1", "is_admin": "1", "group_id": "0"})
    assert r.status_code == 303 and group_name(admin, uid) == ADMIN_GROUP
    await admin.post(f"/admin/users/{uid}", data={"csrf_token": tok, "enabled": "1", "group_id": "0"})
    assert group_name(admin, uid) == settings.default_group


async def test_administrator_group_is_for_administrators_only_and_cannot_be_deleted(admin):
    uid = make_user(admin.sm, "dino", password=GOOD, group="staff")
    tok = await csrf_of_page(admin, "/admin/users")
    with admin.sm() as s:
        gid = s.scalars(select(Group).where(Group.name == ADMIN_GROUP)).one().id
        default_id = s.scalars(select(Group).where(Group.name == settings.default_group)).one().id
    await admin.post(f"/admin/users/{uid}", data={"csrf_token": tok, "enabled": "1", "group_id": str(gid)})
    assert group_name(admin, uid) == "staff"                                    # refused
    for g in (gid, default_id):
        await admin.post(f"/admin/groups/{g}/delete", data={"csrf_token": tok})
    with admin.sm() as s:
        assert {settings.default_group, ADMIN_GROUP} <= {g.name for g in s.scalars(select(Group))}


def test_set_admin_helper(portal):
    uid = make_user(portal.sm, "eli", password=GOOD)
    with portal.sm() as s:
        u = s.get(User, uid)
        set_admin(s, u, True)
        s.commit()
        assert s.get(Group, u.group_id).name == ADMIN_GROUP
        set_admin(s, u, False)
        s.commit()
        assert s.get(Group, u.group_id).name == settings.default_group


# ---------------------------------------------------------------- users page: read only + edit + delete

async def test_list_is_read_only_and_shows_grants_with_a_plus(admin):
    uid = make_user(admin.sm, "fio", password=GOOD, group="staff")
    with admin.sm() as s:
        set_limit(s, SubjectType.user, uid, Metric.pages, 50, Period.monthly)
        from datetime import datetime, timedelta
        now = datetime.now(UTC)
        from spoolsteward.db.models import RequestScope
        s.add(QuotaGrant(user_id=uid, metric=Metric.pages, amount=20, scope=RequestScope.temporary,
                         valid_from=now - timedelta(hours=1), valid_until=now + timedelta(days=3)))
        s.commit()
    page = (await admin.get("/admin/users")).text
    assert "built-in method" not in page and "0 active" in page          # jinja: a dict key named "keys" is the method
    assert "Edit" in page and "Save" not in page and "<select name=\"group_id\" form" not in page
    row = re.search(r"<tr>\s*<td data-l=\"User\"><b>fio</b>.*?</tr>", page, re.S).group(0)
    assert "+20" in row and 'class="extra"' in row and ">50<" in row
    assert "Origin" not in re.search(r"<tr>.*?</tr>", page, re.S).group(0)         # origin moved to the edit page


async def test_edit_page_carries_every_field_including_origin_and_delete(admin):
    uid = make_user(admin.sm, "gus", password=GOOD, group="staff")
    page = (await admin.get(f"/admin/users/{uid}/edit")).text
    for needle in ('name="group_id"', 'name="enabled"', 'name="is_admin"', 'name="jobs_limit"',
                   'name="pages_limit"', 'name="period"', "Delete account", f"/admin/users/{uid}/keys"):
        assert needle in page, needle
    assert 'name="provider"' not in page and "<select name=\"provider\"" not in page   # the origin is only shown, not a field


async def test_origin_switch_local_to_radius_drops_the_password_and_back_issues_a_temporary_one(admin):
    uid = make_user(admin.sm, "hal", password=GOOD)
    tok = await csrf_of_page(admin, "/admin/users")
    await admin.post(f"/admin/users/{uid}", data={"csrf_token": tok, "enabled": "1", "provider": "radius", "group_id": "0"})
    u = user_row(admin, "hal")
    assert u.provider == "radius" and u.password_hash is None
    r = await admin.post(f"/admin/users/{uid}", data={"csrf_token": tok, "enabled": "1", "provider": "local", "group_id": "0"})
    assert "Temporary password" in r.text or "secret" in r.text
    u = user_row(admin, "hal")
    assert u.provider == "local" and u.password_hash and u.must_change_password


async def test_emergency_admin_stays_local(admin):
    uid = make_user(admin.sm, settings.bootstrap_admin_username, password=GOOD, admin=True)
    tok = await csrf_of_page(admin, "/admin/users")
    await admin.post(f"/admin/users/{uid}", data={"csrf_token": tok, "enabled": "1", "is_admin": "1", "provider": "radius"})
    assert user_row(admin, settings.bootstrap_admin_username).provider == "local"


async def test_nobody_can_create_or_turn_into_local_an_account_with_the_emergency_name(admin):
    """The name decides who is exempt from every limit: it can not be taken by an ordinary account."""
    name = settings.bootstrap_admin_username
    tok = await csrf_of_page(admin, "/admin/users")
    await admin.post("/admin/users/local", data={"username": name, "csrf_token": tok, "password": GOOD})
    assert user_row(admin, name) is None
    with admin.sm() as s:
        s.add(User(provider="radius", username=name))
        s.commit()
    uid = user_row(admin, name).id
    await admin.post(f"/admin/users/{uid}", data={"csrf_token": tok, "enabled": "1", "provider": "local", "group_id": "0"})
    assert user_row(admin, name).provider == "radius"


async def test_delete_keeps_the_print_history_readable(admin):
    uid = make_user(admin.sm, "ivo", password=GOOD, group="staff")
    with admin.sm() as s:
        s.add(JobLog(user_id=uid, user_name="ivo", printer="p", title="contract.pdf", state=JobState.completed, pages=3))
        s.add(PrintKey(user_id=uid, label="x", key_prefix="cpk-abcd", key_hash="h" * 64))
        set_limit(s, SubjectType.user, uid, Metric.jobs, 5, Period.weekly)
        s.commit()
    tok = await csrf_of_page(admin, "/admin/users")
    r = await admin.post(f"/admin/users/{uid}/delete", data={"csrf_token": tok, "confirm_name": "wrong"})
    assert user_row(admin, "ivo") is not None                                     # name must match
    r = await admin.post(f"/admin/users/{uid}/delete", data={"csrf_token": tok, "confirm_name": "IVO"})
    assert r.status_code == 303 and user_row(admin, "ivo") is None
    with admin.sm() as s:
        j = s.scalars(select(JobLog).where(JobLog.title == "contract.pdf")).one()
        assert j.user_id is None and j.user_name == "ivo"
        assert s.scalars(select(PrintKey).where(PrintKey.user_id == uid)).first() is None
        assert s.scalars(select(PolicyRule).where(PolicyRule.subject_id == uid, PolicyRule.subject_type == SubjectType.user)).first() is None
        assert "user.delete" in [a.action for a in s.scalars(select(AdminAudit))]
    jobs_page = (await admin.get("/admin/jobs")).text
    assert "contract.pdf" in jobs_page and ">ivo<" in jobs_page                  # still shows who printed it
    assert "ivo" in (await admin.get("/admin/jobs.csv")).text


async def test_delete_is_refused_for_yourself_the_emergency_account_and_the_last_admin(admin):
    me = user_row(admin, "boss").id
    emerg = make_user(admin.sm, settings.bootstrap_admin_username, password=GOOD, admin=True)
    tok = await csrf_of_page(admin, "/admin/users")
    for uid, name in ((me, "boss"), (emerg, settings.bootstrap_admin_username)):
        await admin.post(f"/admin/users/{uid}/delete", data={"csrf_token": tok, "confirm_name": name})
        assert user_row(admin, name) is not None


async def test_deleted_admin_leaves_the_admin_log_readable(admin):
    other = make_user(admin.sm, "jo", password=GOOD, admin=True)
    tok = await csrf_of_page(admin, "/admin/users")
    with admin.sm() as s:
        s.add(AdminAudit(actor_id=other, actor_name="jo", action="x.y", target_type="t", target_id=1))
        s.commit()
    await admin.post(f"/admin/users/{other}/delete", data={"csrf_token": tok, "confirm_name": "jo"})
    with admin.sm() as s:
        row = s.scalars(select(AdminAudit).where(AdminAudit.action == "x.y")).one()
        assert row.actor_id is None and row.actor_name == "jo"


# ---------------------------------------------------------------- emergency administrator: no group, no limits

def test_emergency_admin_has_no_group_and_no_limits(portal):
    name = settings.bootstrap_admin_username
    uid = make_user(portal.sm, name, password=GOOD, admin=True, group=ADMIN_GROUP)
    with portal.sm() as s:
        set_limit(s, SubjectType.user, uid, Metric.jobs, 1, Period.weekly)
        set_limit(s, SubjectType.group, s.scalars(select(Group).where(Group.name == ADMIN_GROUP)).one().id, Metric.jobs, 1, Period.weekly)
        s.add_all([JobLog(user_id=uid, user_name=name, printer="p", state=JobState.completed, pages=2) for _ in range(3)])
        s.commit()
    ensure_system_groups(portal.sm)
    assert group_name(portal, uid) is None                                          # outside every group
    with portal.sm() as s:
        from spoolsteward.policy.engine import check_job, effective_rules
        from spoolsteward.db.models import Resource
        from datetime import datetime
        from zoneinfo import ZoneInfo
        u = s.get(User, uid)
        assert effective_rules(s, u, Resource.print) == []
        assert check_job(s, u, Resource.print, datetime.now(UTC), ZoneInfo("UTC"), pages=500).allowed
        assert s.scalars(select(PolicyRule).where(PolicyRule.subject_type == SubjectType.user, PolicyRule.subject_id == uid)).first() is None


async def test_emergency_admin_group_and_limits_cannot_be_set(admin):
    name = settings.bootstrap_admin_username
    uid = make_user(admin.sm, name, password=GOOD, admin=True)
    ensure_system_groups(admin.sm)
    with admin.sm() as s:
        gid = s.scalars(select(Group).where(Group.name == settings.default_group)).one().id
    tok = await csrf_of_page(admin, "/admin/users")
    await admin.post(f"/admin/users/{uid}", data={"csrf_token": tok, "enabled": "1", "is_admin": "1", "group_id": str(gid),
                                                  "jobs_limit": "3", "pages_limit": "9"})
    assert group_name(admin, uid) is None
    with admin.sm() as s:
        assert s.scalars(select(PolicyRule).where(PolicyRule.subject_type == SubjectType.user, PolicyRule.subject_id == uid)).first() is None
    ensure_system_groups(admin.sm)                                                  # a restart keeps it out of the groups
    assert group_name(admin, uid) is None
    other = make_user(admin.sm, "second", password=GOOD, admin=True)
    ensure_system_groups(admin.sm)
    assert group_name(admin, other) == ADMIN_GROUP                                  # ordinary administrators stay in the group


async def test_local_and_radius_users_can_be_created_in_a_chosen_group(portal, admin, monkeypatch):
    from spoolsteward.config import settings

    monkeypatch.setattr(settings, "radius_host", "radius.test")      # the form is offered only when RADIUS is configured
    monkeypatch.setattr(settings, "radius_secret", "x")
    with portal.sm() as s:
        s.add(Group(name="students"))
        s.add(Group(name="teachers"))
        s.commit()
    csrf = await csrf_of_page(portal, "/admin/users")
    page = (await portal.get("/admin/users")).text
    forms = re.findall(r'<select name="group_id">(.*?)</select>', page, re.S)
    assert len(forms) == 2                                           # local form and RADIUS form
    for f in forms:
        assert "students" in f and "teachers" in f and "administrator" not in f
    with portal.sm() as s:
        gid = {g.name: g.id for g in s.scalars(select(Group))}

    r = await portal.post("/admin/users/local", data=dict(csrf_token=csrf, username="loc1", group_id=gid["students"]))
    assert r.status_code == 200 and "Temporary password" in r.text
    r = await portal.post("/admin/users", data=dict(csrf_token=csrf, username="Rad1", group_id=gid["teachers"]))
    assert r.status_code == 303
    with portal.sm() as s:
        u = {x.username: x for x in s.scalars(select(User))}
        assert u["loc1"].provider == "local" and u["loc1"].group_id == gid["students"]
        assert u["rad1"].provider == "radius" and u["rad1"].group_id == gid["teachers"]
        assert not u["rad1"].is_admin

    # the pre-registered person logs in through RADIUS later and keeps the group chosen for them
    # (the stub RADIUS knows "rad"/"radpw": pre-register that name, then let them in)
    await portal.post("/admin/users", data=dict(csrf_token=csrf, username="rad", group_id=gid["teachers"]))
    await portal.post("/logout", data=dict(csrf_token=csrf))
    login = await portal_login(portal, "rad", "radpw")
    assert login.status_code in (200, 303)
    with portal.sm() as s:
        rad = s.scalars(select(User).where(User.username == "rad")).one()
        assert rad.group_id == gid["teachers"] and rad.provider == "radius"

    # the administrator group is not offered and not accepted
    await portal_login(portal, "boss", GOOD)
    csrf = await csrf_of_page(portal, "/admin/users")
    with portal.sm() as s:
        admin_gid = s.scalars(select(Group).where(Group.name == "administrator")).one().id   # made at start (the admin fixture does it too)
    page = (await portal.get("/admin/users")).text
    assert "administrator" not in " ".join(re.findall(r'<select name="group_id">(.*?)</select>', page, re.S))
    await portal.post("/admin/users/local", data=dict(csrf_token=csrf, username="sneaky", group_id=admin_gid))
    await portal.post("/admin/users", data=dict(csrf_token=csrf, username="sneaky2", group_id=admin_gid))
    with portal.sm() as s:
        assert not s.scalars(select(User).where(User.username.in_(["sneaky", "sneaky2"]))).all()
