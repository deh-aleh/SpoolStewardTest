"""Phase 2: personal print keys and the two IPP authentication modes."""

import base64
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from spoolsteward.auth import keys
from spoolsteward.auth import keys as keylib
from spoolsteward.config import settings
from spoolsteward.db.models import AdminAudit, JobLog, PrintKey, User
from spoolsteward.ipp import codec as c
from tests.conftest import (
    CUPS_TEST_URL, StubRadius, csrf_of_page, make_user, portal_login, requires_cups,
)

GOOD = "correct horse battery"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def basic(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode(), "Content-Type": "application/ipp"}


def new_key(sm, username, label="laptop", ttl=30, now=NOW):
    with sm() as s:
        user = s.scalars(select(User).where(User.username == username)).one()
        row, plain = keys.create_key(s, user, label, ttl, now)
        return row.id, plain


def check(sm, username, plain, now=NOW):
    with sm() as s:
        user = s.scalars(select(User).where(User.username == username)).one()
        return keys.verify_key(s, user, plain, "10.1.1.1", now)


# ---------------------------------------------------------------- the key library

def test_only_the_hash_is_stored_and_the_key_has_enough_entropy(portal):
    make_user(portal.sm, "ann", password=GOOD)
    kid, plain = new_key(portal.sm, "ann")
    assert plain.startswith("cpk-") and len(plain) >= 32
    with portal.sm() as s:
        row = s.get(PrintKey, kid)
        assert plain not in repr(row.__dict__) and row.key_hash == keys.hash_key(plain) and len(row.key_hash) == 64
        assert row.key_prefix == plain[:8]
    assert new_key(portal.sm, "ann")[1] != plain                                      # random each time


def test_a_key_is_valid_for_its_owner_only_and_until_expiry_or_revocation(portal):
    make_user(portal.sm, "ann", password=GOOD)
    make_user(portal.sm, "bob", password=GOOD)
    kid, plain = new_key(portal.sm, "ann", ttl=10)
    assert check(portal.sm, "ann", plain)
    assert not check(portal.sm, "bob", plain)                                         # somebody else's key
    assert not check(portal.sm, "ann", plain + "x") and not check(portal.sm, "ann", "cpk-nope")
    assert not check(portal.sm, "ann", "x" * 200) and not check(portal.sm, "ann", "")
    assert check(portal.sm, "ann", plain, now=NOW + timedelta(days=9, hours=23))
    assert not check(portal.sm, "ann", plain, now=NOW + timedelta(days=10, seconds=1))  # expired
    with portal.sm() as s:
        assert keys.revoke_key(s, s.get(PrintKey, kid)) and not keys.revoke_key(s, s.get(PrintKey, kid))
    assert not check(portal.sm, "ann", plain)                                          # revoked


def test_limits_and_validation(portal):
    make_user(portal.sm, "ann", password=GOOD)
    with portal.sm() as s:
        user = s.scalars(select(User)).one()
        for bad in ("", "   ", "x" * 65):
            with pytest.raises(ValueError):
                keys.create_key(s, user, bad, 30)
        for i in range(keys.MAX_KEYS_PER_USER):
            keys.create_key(s, user, f"k{i}", 30)
        with pytest.raises(ValueError, match="at most"):
            keys.create_key(s, user, "one too many", 30)
        assert keys.revoke_all(s, user) == keys.MAX_KEYS_PER_USER
        keys.create_key(s, user, "fresh", 30)                                         # revoked ones free the slots


def test_last_used_is_recorded_but_not_on_every_request(portal, monkeypatch):
    make_user(portal.sm, "ann", password=GOOD)
    kid, plain = new_key(portal.sm, "ann")
    clock = [1000.0]
    monkeypatch.setattr(keys.time, "monotonic", lambda: clock[0])
    keys._last_touch.clear()
    check(portal.sm, "ann", plain)
    with portal.sm() as s:
        first = s.get(PrintKey, kid).last_used_at
        assert first is not None and s.get(PrintKey, kid).last_used_ip == "10.1.1.1"
    clock[0] += 10
    check(portal.sm, "ann", plain, now=NOW + timedelta(minutes=5))
    with portal.sm() as s:
        assert s.get(PrintKey, kid).last_used_at == first                             # within a minute: no write
    clock[0] += 61
    check(portal.sm, "ann", plain, now=NOW + timedelta(minutes=6))
    with portal.sm() as s:
        assert s.get(PrintKey, kid).last_used_at != first


# ---------------------------------------------------------------- the portal pages

async def test_key_is_shown_once_with_the_suggested_lifetime_per_kind_of_account(portal):
    make_user(portal.sm, "loc", password=GOOD)
    await portal_login(portal, "loc", GOOD)
    token = await csrf_of_page(portal)
    r = await portal.post("/me/keys", data={"label": "my laptop", "csrf_token": token})
    plain = re.search(r'class="secret">([^<]+)<', r.text).group(1)
    assert plain.startswith("cpk-") and "no-store" in r.headers["cache-control"]
    page = (await portal.get("/me/keys")).text
    assert plain not in page and plain[:8] in page and "my laptop" in page            # only the prefix is listed
    with portal.sm() as s:
        row = s.scalars(select(PrintKey)).one()
        assert abs((row.expires_at.replace(tzinfo=UTC) - row.created_at.replace(tzinfo=UTC)).days
                   - 365) <= 1                                                          # the menu suggests one year
    await portal.post("/logout", data={"csrf_token": token})
    await portal_login(portal, "rad", "radpw")                                         # RADIUS-backed: the longest allowed is 90
    token = await csrf_of_page(portal)
    await portal.post("/me/keys", data={"label": "phone", "csrf_token": token})
    with portal.sm() as s:
        row = s.scalars(select(PrintKey).join(User).where(User.username == "rad")).one()
        assert abs((row.expires_at.replace(tzinfo=UTC) - row.created_at.replace(tzinfo=UTC)).days
                   - settings.key_ttl_days_radius) <= 1


async def test_users_revoke_only_their_own_keys(portal):
    make_user(portal.sm, "ann", password=GOOD)
    make_user(portal.sm, "bob", password=GOOD)
    kid_bob, _ = new_key(portal.sm, "bob")
    kid_ann, plain_ann = new_key(portal.sm, "ann")
    await portal_login(portal, "ann", GOOD)
    token = await csrf_of_page(portal)
    assert (await portal.post(f"/me/keys/{kid_bob}/revoke", data={"csrf_token": token})).status_code == 404
    await portal.post(f"/me/keys/{kid_ann}/revoke", data={"csrf_token": token})
    with portal.sm() as s:
        assert s.get(PrintKey, kid_bob).revoked_at is None and s.get(PrintKey, kid_ann).revoked_at is not None
    assert not check(portal.sm, "ann", plain_ann)


async def test_admin_can_revoke_all_keys_of_a_user_and_it_is_logged(portal):
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    make_user(portal.sm, "ann", password=GOOD)
    _, p1 = new_key(portal.sm, "ann", "a")
    _, p2 = new_key(portal.sm, "ann", "b")
    await portal_login(portal, "boss", GOOD)
    token = await csrf_of_page(portal, "/admin/users")
    with portal.sm() as s:
        uid = s.scalars(select(User).where(User.username == "ann")).one().id
    await portal.post(f"/admin/users/{uid}/revoke-keys", data={"csrf_token": token})
    assert not check(portal.sm, "ann", p1) and not check(portal.sm, "ann", p2)
    with portal.sm() as s:
        assert s.scalars(select(AdminAudit).where(AdminAudit.action == "user.revoke_keys")).one().detail_json["revoked"] == 2


async def test_a_print_key_is_not_a_portal_password(portal):
    make_user(portal.sm, "ann", password=GOOD)
    make_user(portal.sm, "radu", provider="radius")
    _, k1 = new_key(portal.sm, "ann")
    _, k2 = new_key(portal.sm, "radu")
    assert (await portal_login(portal, "ann", k1)).headers["location"] == "/login"
    assert (await portal_login(portal, "radu", k2)).headers["location"] == "/login"


# ---------------------------------------------------------------- IPP: who may print, in each mode

@pytest.fixture
async def ipp(portal, monkeypatch):
    monkeypatch.setattr(settings, "ipp_auth", "keys")
    portal.app.state.upstream = httpx.AsyncClient(base_url=CUPS_TEST_URL, timeout=10)
    yield portal
    await portal.app.state.upstream.aclose()


def print_job(title="doc.txt"):
    r = c.make_request(c.PRINT_JOB, 1, printer_uri="ipp://gw.test/printers/virtual", job_name=title,
                       document_format="text/plain")
    r.data = b"hello\n"
    return r


def get_jobs():
    return c.make_request(c.GET_JOBS, 1, printer_uri="ipp://gw.test/printers/virtual")


async def send(cl, req, user, secret):
    return await cl.post("/printers/virtual", content=c.encode(req), headers=basic(user, secret))


@requires_cups
async def test_a_local_user_prints_with_a_key_and_the_job_is_audited(ipp):
    make_user(ipp.sm, "ann", password=GOOD)
    _, plain = new_key(ipp.sm, "ann")
    r = await send(ipp, print_job("budget.txt"), "ann", plain)
    assert r.status_code == 200 and c.decode(r.content).code < 0x0100
    with ipp.sm() as s:
        row = s.scalars(select(JobLog)).one()
        assert (row.title, row.state.value, row.printer) == ("budget.txt", "accepted", "virtual")
        assert s.get(User, row.user_id).username == "ann"


@requires_cups
async def test_the_domain_password_is_not_accepted_for_printing_in_keys_mode(ipp):
    make_user(ipp.sm, "radu", provider="radius")
    ipp.app.state.auth = StubRadius({"radu": "domain-pass"})
    assert (await send(ipp, get_jobs(), "radu", "domain-pass")).status_code == 401
    _, plain = new_key(ipp.sm, "radu")
    assert (await send(ipp, get_jobs(), "radu", plain)).status_code == 200            # the key does


@requires_cups
async def test_keys_plus_radius_mode_accepts_the_domain_password_but_never_for_local_accounts(ipp, monkeypatch):
    monkeypatch.setattr(settings, "ipp_auth", "keys+radius")
    make_user(ipp.sm, "radu", provider="radius")
    make_user(ipp.sm, "loc", password=GOOD)
    ipp.app.state.auth = StubRadius({"radu": "domain-pass", "loc": "domain-pass"})
    assert (await send(ipp, get_jobs(), "radu", "domain-pass")).status_code == 200
    assert (await send(ipp, get_jobs(), "loc", "domain-pass")).status_code == 401     # RADIUS can not speak for a local account
    assert (await send(ipp, get_jobs(), "loc", GOOD)).status_code == 401              # nor does the portal password work here


async def test_every_way_a_key_can_be_wrong_is_just_a_401(ipp):
    make_user(ipp.sm, "ann", password=GOOD)
    make_user(ipp.sm, "bob", password=GOOD)
    make_user(ipp.sm, "off", password=GOOD)
    kid, ann_key = new_key(ipp.sm, "ann")
    _, off_key = new_key(ipp.sm, "off")
    _, expired = new_key(ipp.sm, "bob", ttl=1, now=NOW - timedelta(days=5))
    with ipp.sm() as s:
        s.query(User).filter_by(username="off").update({"enabled": False}); s.commit()
    cases = [("bob", ann_key), ("ghost", ann_key), ("ann", "cpk-wrong"), ("ann", ""), ("bob", expired), ("off", off_key)]
    for user, secret in cases:
        assert (await send(ipp, get_jobs(), user, secret)).status_code == 401, (user, secret)
    with ipp.sm() as s:
        assert s.scalars(select(User).where(User.username == "ghost")).first() is None   # keys mode never auto-creates
        s.query(PrintKey).filter_by(id=kid).update({"revoked_at": NOW}); s.commit()
    assert (await send(ipp, get_jobs(), "ann", ann_key)).status_code == 401


async def test_guessing_keys_is_throttled(ipp):
    make_user(ipp.sm, "ann", password=GOOD)
    for i in range(5):
        assert (await send(ipp, get_jobs(), "ann", f"cpk-guess-{i}")).status_code == 401
    assert (await send(ipp, get_jobs(), "ann", "cpk-guess-again")).status_code == 429


# ---------------------------------------------------------------- the lifetime is chosen by the user

def user_of(sm, name):
    with sm() as s:
        return s.scalars(select(User).where(User.username == name)).one()


def test_the_menu_offers_only_what_the_account_may_have(portal, monkeypatch):
    make_user(portal.sm, "loc", password=GOOD)
    make_user(portal.sm, "rad2", provider="radius")
    loc, rad = user_of(portal.sm, "loc"), user_of(portal.sm, "rad2")
    assert keys.allowed_durations(loc) == [30, 90, 180, 365, 548, 730] and keys.default_duration(loc) == 365
    assert keys.allowed_durations(rad) == [30, 90] and keys.default_duration(rad) == 90      # 90 days is the RADIUS maximum
    assert [v for v, _l in keys.duration_choices(rad)] == ["30", "90", "once"]
    monkeypatch.setattr(settings, "key_ttl_days_radius", 10)
    assert keys.allowed_durations(rad) == [10]                                              # even the shortest does not fit: the maximum
    assert keys.parse_duration(loc, "180") == (180, False) and keys.parse_duration(loc, "once") == (None, True)
    for bad in ("", "7", "731", "-30", "1e3", "30 ", "once "):
        with pytest.raises(ValueError):
            keys.parse_duration(loc, bad)
    with pytest.raises(ValueError):
        keys.parse_duration(rad, "365")                                                     # above the RADIUS maximum


async def test_the_chosen_validity_is_applied_and_a_forged_one_is_refused(portal):
    make_user(portal.sm, "loc", password=GOOD)
    await portal_login(portal, "loc", GOOD)
    token = await csrf_of_page(portal)
    await portal.post("/me/keys", data={"label": "six", "validity": "180", "csrf_token": token})
    await portal.post("/me/keys", data={"label": "forged", "validity": "99999", "csrf_token": token})
    with portal.sm() as s:
        rows = s.scalars(select(PrintKey)).all()
        assert [r.label for r in rows] == ["six"]
        assert abs((rows[0].expires_at - rows[0].created_at).days - 180) <= 1


def test_states_and_a_switched_off_key_keeps_ageing(portal):
    make_user(portal.sm, "ann", password=GOOD)
    kid, plain = new_key(portal.sm, "ann", ttl=10)
    with portal.sm() as s:
        row = s.get(PrintKey, kid)
        expires = row.expires_at
        assert keys.key_state(row, NOW) == "active"
        assert keys.disable_key(s, row, NOW) and not keys.disable_key(s, row, NOW)
        assert keys.key_state(row, NOW) == "disabled" and row.expires_at == expires        # the clock is not stopped
        assert keys.verify_key(s, s.get(User, row.user_id), plain, now=NOW) is None
        assert keys.key_state(row, NOW + timedelta(days=11)) == "expired"                   # still ageing while off
        assert keys.enable_key(s, row) and keys.key_state(row, NOW) == "active"
        assert keys.verify_key(s, s.get(User, row.user_id), plain, now=NOW) is not None
        assert keys.revoke_key(s, row, NOW) and keys.key_state(row, NOW) == "revoked"
        assert not keys.enable_key(s, row) and not keys.disable_key(s, row)


def test_extending_gives_a_new_date_and_rearms_the_warning(portal):
    make_user(portal.sm, "ann", password=GOOD)
    kid, _p = new_key(portal.sm, "ann", ttl=10)
    with portal.sm() as s:
        row = s.get(PrintKey, kid)
        row.expiry_notified_at = NOW
        assert keys.extend_key(s, row, 90, NOW + timedelta(days=5))
        assert abs((row.expires_at.replace(tzinfo=UTC) - (NOW + timedelta(days=95))).total_seconds()) < 1
        assert row.expiry_notified_at is None
        keys.revoke_key(s, row, NOW)
        assert not keys.extend_key(s, row, 30)                                              # a revoked key stays revoked


def test_a_single_use_key_is_spent_once_and_the_race_has_one_winner(portal):
    make_user(portal.sm, "ann", password=GOOD)
    with portal.sm() as s:
        user = s.scalars(select(User).where(User.username == "ann")).one()
        row, plain = keys.create_key(s, user, "once", None, NOW, once=True)
        assert row.single_use and (row.expires_at.replace(tzinfo=UTC) - NOW) == timedelta(hours=settings.key_once_hours)
        assert keys.verify_key(s, user, plain, now=NOW)
        assert keys.consume(s, row, NOW) and not keys.consume(s, row, NOW)                  # the second caller loses
        assert keys.key_state(row, NOW) == "used"
        assert keys.verify_key(s, user, plain, now=NOW) is None                              # no new job
        assert keys.verify_key(s, user, plain, now=NOW, read_only=True) is not None          # status queries still work...
        late = NOW + timedelta(seconds=settings.key_once_grace_seconds + 1)
        assert keys.verify_key(s, user, plain, now=late, read_only=True) is None             # ...for a short while only
        assert keys.listing([row], NOW) == []                                                # and it is not in the list
        assert not keys.consume(s, keys.create_key(s, user, "normal", 30, NOW)[0])           # normal keys are never spent


def test_old_keys_leave_the_list_after_the_history_period(portal, monkeypatch):
    make_user(portal.sm, "ann", password=GOOD)
    revoked, _a = new_key(portal.sm, "ann", "revoked", ttl=300)
    expired, _b = new_key(portal.sm, "ann", "expired", ttl=1)
    live, _c = new_key(portal.sm, "ann", "live", ttl=300)
    spent = new_key(portal.sm, "ann", "spent", ttl=300)[0]
    with portal.sm() as s:
        keys.revoke_key(s, s.get(PrintKey, revoked), NOW)
        s.get(PrintKey, spent).single_use, s.get(PrintKey, spent).used_at = True, NOW
        s.commit()
        day = timedelta(days=1)
        assert keys.purge(s, NOW + 13 * day) == 1                                              # only the single-use one (grace over)
        assert keys.purge(s, NOW + 15 * day) == 2                                              # revoked and expired: 14 days later
        assert [k.label for k in s.scalars(select(PrintKey))] == ["live"]
        monkeypatch.setattr(settings, "key_history_days", 0)
        assert keys.purge(s, NOW + 900 * day) == 0                                             # 0 = they stay


def test_the_list_says_when_a_finished_key_will_leave(portal):
    make_user(portal.sm, "ann", password=GOOD)
    kid, _p = new_key(portal.sm, "ann")
    with portal.sm() as s:
        row = s.get(PrintKey, kid)
        keys.revoke_key(s, row, NOW)
        item = keys.listing([row], NOW)[0]
        assert item["state"] == "revoked" and item["vanishes"] == NOW + timedelta(days=14)


# ---------------------------------------------------------------- single-use keys at the gateway

def new_once(sm, username, label="once"):
    with sm() as s:
        user = s.scalars(select(User).where(User.username == username)).one()
        row, plain = keys.create_key(s, user, label, None, once=True)
        return row.id, plain


def used_at(sm, kid):
    with sm() as s:
        return s.get(PrintKey, kid).used_at


@requires_cups
async def test_a_single_use_key_prints_once_then_only_answers_status_queries(ipp, monkeypatch):
    make_user(ipp.sm, "ann", password=GOOD)
    kid, plain = new_once(ipp.sm, "ann")
    val = c.make_request(c.VALIDATE_JOB, 1, printer_uri="ipp://gw.test/printers/virtual", document_format="text/plain")
    assert (await send(ipp, val, "ann", plain)).status_code == 200
    assert used_at(ipp.sm, kid) is None                                                  # asking is not printing
    assert (await send(ipp, print_job("one.txt"), "ann", plain)).status_code == 200
    assert used_at(ipp.sm, kid) is not None                                              # the print spent it
    assert (await send(ipp, print_job("two.txt"), "ann", plain)).status_code == 401      # no second job
    assert (await send(ipp, val, "ann", plain)).status_code == 401
    assert (await send(ipp, get_jobs(), "ann", plain)).status_code == 200                # status: still fine for a moment
    monkeypatch.setattr(settings, "key_once_grace_seconds", 0)
    assert (await send(ipp, get_jobs(), "ann", plain)).status_code == 401                # then it is gone
    with ipp.sm() as s:
        assert [r.title for r in s.scalars(select(JobLog))] == ["one.txt"]               # exactly one job was audited


@requires_cups
async def test_a_job_refused_by_the_quota_does_not_spend_the_single_use_key(ipp):
    from spoolsteward.db.models import Metric, Period, SubjectType
    from spoolsteward.policy.rules import set_limit

    make_user(ipp.sm, "ann", password=GOOD)
    kid, plain = new_once(ipp.sm, "ann")
    with ipp.sm() as s:
        set_limit(s, SubjectType.user, user_of(ipp.sm, "ann").id, Metric.jobs, 0, Period.weekly)
        s.commit()
    r = await send(ipp, print_job(), "ann", plain)
    assert r.status_code == 200 and c.decode(r.content).code >= 0x0400                   # "not possible": over the limit
    assert used_at(ipp.sm, kid) is None


@requires_cups
async def test_two_prints_racing_with_one_single_use_key_have_one_winner(ipp):
    import asyncio

    make_user(ipp.sm, "ann", password=GOOD)
    kid, plain = new_once(ipp.sm, "ann")
    first, second = await asyncio.gather(send(ipp, print_job("a.txt"), "ann", plain), send(ipp, print_job("b.txt"), "ann", plain))
    assert sorted([first.status_code, second.status_code]) == [200, 401]
    with ipp.sm() as s:
        done = [r for r in s.scalars(select(JobLog)) if r.state.value == "accepted"]
        assert len(done) == 1                                                            # the loser's reservation was closed as failed


# ---------------------------------------------------------------- keys

async def _two_keys(portal, uid):
    with portal.sm() as s:
        u = s.get(User, uid)
        a, _ = keylib.create_key(s, u, "phone", 30)
        b, _ = keylib.create_key(s, u, "laptop", 30)
        return a.id, b.id


async def test_admin_can_revoke_one_key_or_all(admin):
    uid = make_user(admin.sm, "kim", password=GOOD)
    k1, k2 = await _two_keys(admin, uid)
    page = (await admin.get(f"/admin/users/{uid}/keys")).text
    assert "phone" in page and "laptop" in page and "Revoke all" in page
    tok = await csrf_of_page(admin, f"/admin/users/{uid}/keys")
    await admin.post(f"/admin/users/{uid}/keys/{k1}/revoke", data={"csrf_token": tok})
    with admin.sm() as s:
        assert s.get(PrintKey, k1).revoked_at is not None and s.get(PrintKey, k2).revoked_at is None
    await admin.post(f"/admin/users/{uid}/revoke-keys", data={"csrf_token": tok})
    with admin.sm() as s:
        assert s.get(PrintKey, k2).revoked_at is not None


async def test_admin_cannot_revoke_a_key_through_another_users_url(admin):
    a, b = make_user(admin.sm, "lia", password=GOOD), make_user(admin.sm, "max", password=GOOD)
    k, _ = await _two_keys(admin, a)
    tok = await csrf_of_page(admin, "/admin/users")
    r = await admin.post(f"/admin/users/{b}/keys/{k}/revoke", data={"csrf_token": tok})
    assert r.status_code == 404
    with admin.sm() as s:
        assert s.get(PrintKey, k).revoked_at is None


async def test_user_manages_own_keys_one_by_one_or_all(portal):
    uid = make_user(portal.sm, "nina", password=GOOD)
    other = make_user(portal.sm, "omar", password=GOOD)
    k1, k2 = await _two_keys(portal, uid)
    foreign, _ = await _two_keys(portal, other)
    await portal_login(portal, "nina", GOOD)
    assert "laptop" in (await portal.get("/me/keys")).text
    tok = await csrf_of_page(portal, "/me/keys")
    assert (await portal.post(f"/me/keys/{foreign}/revoke", data={"csrf_token": tok})).status_code == 404
    await portal.post(f"/me/keys/{k1}/revoke", data={"csrf_token": tok})
    with portal.sm() as s:
        assert s.get(PrintKey, k1).revoked_at and not s.get(PrintKey, k2).revoked_at
    await portal.post("/me/keys/revoke-all", data={"csrf_token": tok})
    with portal.sm() as s:
        assert s.get(PrintKey, k2).revoked_at
