"""Administrators switch off, extend and revoke keys; the owner finds it in their own bell; old keys are tidied up."""

import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import notices
from app.auth import keys
from app.config import settings
from app.db.models import AdminAudit, Notice, PrintKey, User
from app.keywatch import run_once, warn_expiring
from tests.conftest import csrf_of_page, make_user, portal_login

GOOD = "correct horse battery"


def uid(cl, name):
    with cl.sm() as s:
        return s.scalars(select(User).where(User.username == name)).one().id


def key_of(cl, name, label="laptop", ttl=30):
    with cl.sm() as s:
        user = s.scalars(select(User).where(User.username == name)).one()
        row, plain = keys.create_key(s, user, label, ttl)
        return row.id, plain


def own_notices(cl, name, open_only=True):
    with cl.sm() as s:
        q = select(Notice).where(Notice.user_id == uid(cl, name)).order_by(Notice.id)
        return [n for n in s.scalars(q) if not open_only or n.dismissed_at is None]


@pytest.fixture
async def boss(portal):
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    make_user(portal.sm, "ann", password=GOOD)
    await portal_login(portal, "boss", GOOD)
    portal.token = await csrf_of_page(portal, "/admin/users")
    return portal


async def act(cl, ann, kid, what, **form):
    return await cl.post(f"/admin/users/{uid(cl, 'ann')}/keys/{kid}/{what}", data={"csrf_token": cl.token, **form})


async def test_an_admin_revokes_and_the_owner_is_told_but_the_admin_bell_stays_clean(boss):
    kid, _p = key_of(boss, "ann", "my laptop")
    await act(boss, "ann", kid, "revoke")
    assert [n.kind for n in own_notices(boss, "ann")] == ["key_revoked"]
    assert own_notices(boss, "ann")[0].detail_json["label"] == "my laptop"
    with boss.sm() as s:
        assert s.get(PrintKey, kid).revoked_at is not None
    assert notices.unseen_count(boss.sm, uid(boss, "boss")) == 0                          # not for the administrators
    assert "key.revoke" in [a.action for a in boss.sm().scalars(select(AdminAudit))]
    with boss.sm() as s:
        assert s.scalars(select(Notice).where(Notice.user_id.is_(None))).all() == []
    assert (await boss.get("/admin/notices?all=1")).status_code == 200


async def test_disable_enable_extend_each_tell_the_owner_and_are_audited(boss):
    kid, _p = key_of(boss, "ann", "phone", ttl=30)
    with boss.sm() as s:
        before = s.get(PrintKey, kid).expires_at
    await act(boss, "ann", kid, "disable")
    with boss.sm() as s:
        row = s.get(PrintKey, kid)
        assert row.disabled_at is not None and row.expires_at == before                    # the expiry date did not move
    await act(boss, "ann", kid, "enable")
    await act(boss, "ann", kid, "extend", validity="365")
    with boss.sm() as s:
        assert s.get(PrintKey, kid).disabled_at is None and (s.get(PrintKey, kid).expires_at - before).days > 300
    assert [n.kind for n in own_notices(boss, "ann")] == ["key_disabled", "key_enabled", "key_extended"]
    actions = [a.action for a in boss.sm().scalars(select(AdminAudit))]
    assert {"key.disable", "key.enable", "key.extend"} <= set(actions)


async def test_extending_cannot_go_beyond_what_the_account_may_have(boss):
    make_user(boss.sm, "rad", provider="radius")
    with boss.sm() as s:
        user = s.scalars(select(User).where(User.username == "rad")).one()
        kid, _p = keys.create_key(s, user, "x", 30)[0].id, None
    await boss.post(f"/admin/users/{uid(boss, 'rad')}/keys/{kid}/extend", data={"csrf_token": boss.token, "validity": "730"})
    with boss.sm() as s:
        assert (s.get(PrintKey, kid).expires_at - s.get(PrintKey, kid).created_at).days <= 31   # refused, not changed
    assert own_notices(boss, "rad") == []


async def test_revoking_every_key_sends_one_notice_with_the_number(boss):
    key_of(boss, "ann", "a"); key_of(boss, "ann", "b")
    await boss.post(f"/admin/users/{uid(boss, 'ann')}/revoke-keys", data={"csrf_token": boss.token})
    got = own_notices(boss, "ann")
    assert [n.kind for n in got] == ["key_revoked_many"] and got[0].detail_json["count"] == 2


async def test_an_admin_acting_on_their_own_key_gets_no_notice(boss):
    kid, _p = key_of(boss, "boss", "mine")
    await boss.post(f"/admin/users/{uid(boss, 'boss')}/keys/{kid}/revoke", data={"csrf_token": boss.token})
    assert own_notices(boss, "boss") == []


@pytest.mark.usefixtures("english")
async def test_the_owner_sees_their_notices_and_nobody_else_does(boss):
    kid, _p = key_of(boss, "ann", "my laptop")
    await act(boss, "ann", kid, "revoke")
    make_user(boss.sm, "bob", password=GOOD)
    nid = own_notices(boss, "ann")[0].id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boss.app, client=("10.0.0.9", 1)), base_url="https://gw.test") as ann:
        await ann.post("/login", data={"username": "ann", "password": GOOD})
        assert notices.unseen_count(boss.sm, uid(boss, "ann"), own=True) == 1
        page = await ann.get("/me/notices")
        assert page.status_code == 200 and "my laptop" in page.text and "revoked your print key" in page.text
        assert notices.unseen_count(boss.sm, uid(boss, "ann"), own=True) == 0              # opening the list = seen
        assert 'class="bell' in (await ann.get("/me")).text                                # ordinary users have the bell
        assert (await ann.get("/admin/notices")).status_code in (303, 403)                  # the administrators' list is not theirs
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        assert (await ann.post(f"/me/notices/{nid}/dismiss", data={"csrf_token": token})).status_code == 303
    assert own_notices(boss, "ann") == []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boss.app, client=("10.0.0.10", 1)), base_url="https://gw.test") as bob:
        await bob.post("/login", data={"username": "bob", "password": GOOD})
        assert "my laptop" not in (await bob.get("/me/notices?all=1")).text
        t = re.search(r'name="csrf_token" value="([^"]+)"', (await bob.get("/me/notices")).text)
        tok = t.group(1) if t else (await csrf_of_page(boss, "/me"))
        assert (await bob.post(f"/me/notices/{nid}/dismiss", data={"csrf_token": tok})).status_code in (403, 404)


async def test_the_administrators_list_cannot_dismiss_a_users_notice(boss):
    kid, _p = key_of(boss, "ann", "x")
    await act(boss, "ann", kid, "revoke")
    nid = own_notices(boss, "ann")[0].id
    assert (await boss.post(f"/admin/notices/{nid}/dismiss", data={"csrf_token": boss.token})).status_code == 404
    await boss.post("/admin/notices/dismiss-all", data={"csrf_token": boss.token})
    assert len(own_notices(boss, "ann")) == 1                                              # "dismiss all" is the admins' own


async def test_the_users_own_revoke_tells_nobody(boss):
    kid, _p = key_of(boss, "ann", "x")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boss.app, client=("10.0.0.9", 1)), base_url="https://gw.test") as ann:
        await ann.post("/login", data={"username": "ann", "password": GOOD})
        token = await csrf_of_page(ann, "/me/keys")
        await ann.post(f"/me/keys/{kid}/revoke", data={"csrf_token": token})
    assert own_notices(boss, "ann") == []


async def test_the_key_pages_show_the_new_buttons_and_the_menu(boss):
    kid, _p = key_of(boss, "ann", "laptop")
    page = (await boss.get(f"/admin/users/{uid(boss, 'ann')}/keys")).text
    assert f"/keys/{kid}/disable" in page and f"/keys/{kid}/extend" in page and "/revoke-keys" in page
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boss.app, client=("10.0.0.9", 1)), base_url="https://gw.test") as ann:
        await ann.post("/login", data={"username": "ann", "password": GOOD})
        mine = (await ann.get("/me/keys")).text
        assert 'name="validity"' in mine and '<option value="365" selected>' in mine and 'value="once"' in mine
        assert "/disable" not in mine and "/extend" not in mine                             # only administrators do those


# ---------------------------------------------------------------- the hourly housekeeping

def test_the_expiry_warning_is_off_by_default_and_sent_once_when_on(portal, monkeypatch):
    make_user(portal.sm, "ann", password=GOOD)
    kid, _p = key_of(portal, "ann", "soon", ttl=5)
    key_of(portal, "ann", "later", ttl=300)
    assert settings.key_expiry_warn_enabled is False
    run_once(portal.sm)
    assert own_notices(portal, "ann") == []
    monkeypatch.setattr(settings, "key_expiry_warn_enabled", True)
    run_once(portal.sm)
    run_once(portal.sm)
    got = own_notices(portal, "ann")
    assert [n.kind for n in got] == ["key_expiring"] and got[0].detail_json["label"] == "soon"
    with portal.sm() as s:
        keys.extend_key(s, s.get(PrintKey, kid), 5)                                         # extending re-arms it
    assert warn_expiring(portal.sm) == 1


def test_housekeeping_forgets_old_keys(portal):
    make_user(portal.sm, "ann", password=GOOD)
    kid, _p = key_of(portal, "ann", "old")
    with portal.sm() as s:
        keys.revoke_key(s, s.get(PrintKey, kid), datetime.now(UTC) - timedelta(days=15))
    assert run_once(portal.sm) == (1, 0)
    with portal.sm() as s:
        assert s.scalars(select(PrintKey)).all() == []


def test_a_user_notice_is_capped_so_it_can_not_grow_for_ever(portal):
    make_user(portal.sm, "ann", password=GOOD)
    for i in range(notices.MAX_OPEN_PER_USER + 5):
        notices.notify_user(portal.sm, uid(portal, "ann"), "key_revoked", {"label": str(i), "prefix": "cpk-x"})
    assert len(own_notices(portal, "ann")) == notices.MAX_OPEN_PER_USER


def test_a_repeated_event_renews_one_notice_and_counts_it(portal):
    make_user(portal.sm, "ann", password=GOOD)
    for _ in range(3):
        notices.notify_user_counted(portal.sm, uid(portal, "ann"), "job_adjusted", "adjusted:hp", {"printer": "hp", "changed": ["color"]})
    [n] = own_notices(portal, "ann")
    assert n.count == 3
    with portal.sm() as s:   # once dismissed, the next event opens a new notice
        s.get(Notice, n.id).dismissed_at = datetime.now(UTC)
        s.commit()
    notices.notify_user_counted(portal.sm, uid(portal, "ann"), "job_adjusted", "adjusted:hp", {"printer": "hp", "changed": ["color"]})
    assert [x.count for x in own_notices(portal, "ann")] == [1]


def test_secondary_notices_are_read_after_the_chosen_time_and_others_are_not(portal):
    make_user(portal.sm, "ann", password=GOOD)
    a = uid(portal, "ann")
    notices.notify_user_counted(portal.sm, a, "job_adjusted", "adjusted:hp", {"printer": "hp", "changed": ["color"]})
    notices.notify_user_counted(portal.sm, a, "quota_reached", "weekly:jobs", {"printer": "hp", "period": "weekly", "metric": "jobs", "used": 5, "limit": 5})
    soon, later = datetime.now(UTC) + timedelta(minutes=10), datetime.now(UTC) + timedelta(minutes=25)
    assert notices.read_secondary(portal.sm, a, 20, now=soon) == 0
    assert notices.read_secondary(portal.sm, a, 0, now=later) == 0            # 0 = never
    assert notices.read_secondary(portal.sm, a, 20, now=later) == 1
    assert [n.kind for n in own_notices(portal, "ann")] == ["quota_reached"]  # a quota notice is not secondary
    with portal.sm() as s:
        assert s.scalars(select(Notice).where(Notice.kind == "job_adjusted")).one().dismissed_by == "auto"


async def test_the_delay_for_secondary_notices_is_a_setting(portal):
    make_user(portal.sm, "ann", password=GOOD)
    await portal_login(portal, "ann", GOOD)
    tok = await csrf_of_page(portal, "/me/settings")
    with portal.sm() as s:
        assert s.scalars(select(User).where(User.username == "ann")).one().secondary_read_minutes == 20   # default
    for bad in ("-1", "abc", "99999999"):
        await portal.post("/me/settings/notices", data={"csrf_token": tok, "minutes": bad})
    await portal.post("/me/settings/notices", data={"csrf_token": tok, "minutes": "45"})
    with portal.sm() as s:
        assert s.scalars(select(User).where(User.username == "ann")).one().secondary_read_minutes == 45


async def test_login_lives_at_the_root(portal):
    assert (await portal.get("/login")).status_code == 200
