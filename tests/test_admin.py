from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from spoolsteward.auth.throttle import Throttle
from spoolsteward.db.models import AdminAudit, Base, Group, JobKind, JobLog, JobState, Metric, PolicyRule, User
from spoolsteward.main import app
from tests.conftest import csrf_from


class StubAuth:
    name = "radius"

    async def authenticate(self, u, p):
        return {"boss": "pw", "mario": "pw1", "boss2": "pw"}.get(u) == p


@pytest.fixture
async def env():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sm = sessionmaker(engine, expire_on_commit=False)
    with sm() as s:
        g = Group(name="staff"); s.add(g); s.flush()
        s.add_all([User(username="boss", is_admin=True), User(username="boss2", is_admin=True),
                   User(username="mario", group_id=g.id)])
        s.commit()
    app.state.sessionmaker, app.state.auth, app.state.throttle = sm, StubAuth(), Throttle(5, 300)
    transport = httpx.ASGITransport(app=app, client=("10.0.0.9", 1))
    async with httpx.AsyncClient(transport=transport, base_url="https://gw.test") as cl:
        cl.sm = sm
        yield cl


async def login(cl, user="boss", pw="pw"):
    r = await cl.post("/login", data={"username": user, "password": pw})
    return r


async def token(cl):
    return csrf_from((await cl.get("/admin/users")).text)


def q(cl, model, **kw):
    with cl.sm() as s:
        return s.query(model).filter_by(**kw).first()


async def test_requires_login(env):
    r = await env.get("/admin/jobs")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (await env.get("/login")).status_code == 200


async def test_login_rules(env):
    assert (await login(env, "boss", "bad")).headers["location"] == "/login"
    r = await login(env, "mario", "pw1")                                              # valid RADIUS, not an admin:
    assert r.headers["location"] == "/me"                                              # he gets his own page...
    assert (await env.get("/admin/jobs")).status_code == 403                           # ...and not the admin ones
    assert (await env.get("/admin/users")).status_code == 403
    await env.post("/logout", data={"csrf_token": csrf_from((await env.get("/me")).text)})
    assert (await login(env, "ghost", "x")).headers["location"] == "/login"
    assert (await env.get("/admin/jobs")).status_code == 303
    assert (await login(env)).headers["location"] == "/admin/"
    assert (await env.get("/admin/jobs")).status_code == 200


async def test_login_throttled(env):
    for i in range(5):
        await login(env, "boss", f"bad{i}")      # five different guesses
    r = await login(env)  # correct password, but locked out
    assert r.headers["location"] == "/login"
    assert (await env.get("/admin/jobs")).status_code == 303


async def test_post_without_csrf_rejected(env):
    await login(env)
    r = await env.post("/admin/groups", data={"name": "evil"})
    assert r.status_code == 403
    r = await env.post("/admin/groups", data={"name": "evil", "csrf_token": "wrong"})
    assert r.status_code == 403
    r = await env.post("/admin/groups", data={"name": "evil", "csrf_token": "caf\u00e9 \u65e5\u672c"})    # not ASCII: a refusal, not a 500
    assert r.status_code == 403
    assert q(env, Group, name="evil") is None


async def test_group_crud_and_limits(env):
    await login(env)
    t = await token(env)
    await env.post("/admin/groups", data={"name": "interns", "csrf_token": t})
    g = q(env, Group, name="interns")
    await env.post(f"/admin/groups/{g.id}", data={"jobs_limit": "10", "pages_limit": "100", "period": "monthly", "csrf_token": t})
    with env.sm() as s:
        rules = {r.metric: (r.limit, r.period.value) for r in s.query(PolicyRule).filter_by(subject_id=g.id)}
    assert rules == {Metric.jobs: (10, "monthly"), Metric.pages: (100, "monthly")}
    await env.post(f"/admin/groups/{g.id}", data={"jobs_limit": "", "pages_limit": "5", "period": "weekly", "csrf_token": t})
    with env.sm() as s:
        assert [(r.metric, r.limit) for r in s.query(PolicyRule).filter_by(subject_id=g.id)] == [(Metric.pages, 5)]
    await env.post(f"/admin/groups/{g.id}", data={"jobs_limit": "abc", "csrf_token": t})
    assert "invalid limit" in (await env.get("/admin/groups")).text
    await env.post(f"/admin/groups/{g.id}/delete", data={"csrf_token": t})
    assert q(env, Group, name="interns") is None
    with env.sm() as s:
        assert s.query(PolicyRule).filter_by(subject_id=g.id).count() == 0


async def test_deleting_a_group_with_members_moves_them_to_the_default_group(env):
    await login(env)
    t = await token(env)
    gid = q(env, Group, name="staff").id
    page = (await env.get("/admin/groups")).text
    assert "has 1 user" in page and "default" in page                            # the warning names the consequence
    await env.post(f"/admin/groups/{gid}/delete", data={"csrf_token": t})
    assert q(env, Group, name="staff") is None
    default = q(env, Group, name="default")
    assert default is not None and q(env, User, username="mario").group_id == default.id
    with env.sm() as s:
        entry = s.query(AdminAudit).filter_by(action="group.delete").one()
    assert entry.detail_json["moved_users"] == ["mario"] and entry.detail_json["moved_to"] == "default"


async def test_groups_page_is_read_only_and_edit_page_does_the_rest(env):
    await login(env)
    t = await token(env)
    gid = q(env, Group, name="staff").id
    page = (await env.get("/admin/groups")).text
    assert 'name="jobs_limit"' not in page and f'href="/admin/groups/{gid}/edit"' in page and "New group" in page
    edit = (await env.get(f"/admin/groups/{gid}/edit")).text
    assert 'name="jobs_limit"' in edit and 'name="name"' in edit and "Delete" in edit
    assert 'name="name"' in (await env.get("/admin/groups/new")).text
    await env.post("/admin/groups", data={"name": "interns", "jobs_limit": "4", "period": "monthly", "requestable": "1", "csrf_token": t})
    g = q(env, Group, name="interns")
    assert g.requestable
    await env.post(f"/admin/groups/{g.id}", data={"name": "trainees", "jobs_limit": "4", "period": "monthly", "csrf_token": t})
    assert q(env, Group, name="trainees") is not None and q(env, Group, name="interns") is None
    await env.post(f"/admin/groups/{gid}", data={"name": "trainees", "csrf_token": t})                 # name already taken
    assert q(env, Group, name="staff") is not None


async def test_user_update_and_override(env):
    await login(env)
    t = await token(env)
    m = q(env, User, username="mario")
    await env.post(f"/admin/users/{m.id}", data={"group_id": str(m.group_id), "enabled": "1", "jobs_limit": "3",
                                                 "pages_limit": "", "period": "weekly", "csrf_token": t})
    with env.sm() as s:
        assert [(r.metric, r.limit) for r in s.query(PolicyRule).filter_by(subject_id=m.id, subject_type="user")] == [(Metric.jobs, 3)]
    await env.post(f"/admin/users/{m.id}", data={"group_id": "0", "jobs_limit": "", "csrf_token": t})  # unchecked boxes
    assert q(env, User, username="mario").enabled is False and q(env, User, username="mario").group_id is None


async def test_cannot_demote_or_disable_self(env):
    await login(env)
    t = await token(env)
    me = q(env, User, username="boss")
    await env.post(f"/admin/users/{me.id}", data={"enabled": "1", "csrf_token": t})  # is_admin unchecked
    assert q(env, User, username="boss").is_admin is True
    await env.post(f"/admin/users/{me.id}", data={"is_admin": "1", "csrf_token": t})  # enabled unchecked
    assert q(env, User, username="boss").enabled is True


async def test_disabled_admin_loses_access_immediately(env):
    await login(env)
    assert (await env.get("/admin/jobs")).status_code == 200
    with env.sm() as s:
        s.query(User).filter_by(username="boss").update({"enabled": False}); s.commit()
    assert (await env.get("/admin/jobs")).status_code == 303


async def test_create_user_normalizes_and_rejects_duplicates(env):
    await login(env)
    t = await token(env)
    await env.post("/admin/users", data={"username": "  NewGuy ", "group_id": "0", "csrf_token": t})
    assert q(env, User, username="newguy") is not None
    await env.post("/admin/users", data={"username": "newguy", "group_id": "0", "csrf_token": t})
    with env.sm() as s:
        assert s.query(User).filter_by(username="newguy").count() == 1


def add_job(cl, title, user="mario", state=JobState.completed, reason=None):
    with cl.sm() as s:
        uid = s.query(User).filter_by(username=user).one().id
        s.add(JobLog(ts=datetime.now(UTC), user_id=uid, kind=JobKind.print, printer="p",
                     title=title, state=state, denied_reason=reason, pages=2))
        s.commit()


async def test_titles_are_escaped(env):
    add_job(env, "<script>alert(1)</script>.pdf")
    await login(env)
    html = (await env.get("/admin/jobs")).text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


async def test_filters_and_csv_injection(env):
    add_job(env, "=HYPERLINK(\"http://evil\")")
    add_job(env, "report.pdf")
    add_job(env, "blocked.pdf", state=JobState.denied, reason="weekly job limit reached (3/3)")
    await login(env)
    html = (await env.get("/admin/jobs", params={"state": "denied"})).text
    assert "blocked.pdf" in html and "report.pdf" not in html
    assert "report.pdf" in (await env.get("/admin/jobs", params={"q": "repo"})).text
    assert "report.pdf" not in (await env.get("/admin/jobs", params={"user": "nobody"})).text
    csv_text = (await env.get("/admin/jobs.csv")).text
    assert "'=HYPERLINK" in csv_text and ",=HYPERLINK" not in csv_text


async def test_logout_invalidates_session(env):
    await login(env)
    t = await token(env)
    await env.post("/logout", data={"csrf_token": t})
    assert (await env.get("/admin/jobs")).status_code == 303


async def test_docs_need_a_login(env):
    r = await env.get("/docs", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_unknown_group_and_absurd_limits_are_rejected(env):
    await login(env)
    t = await token(env)
    m = q(env, User, username="mario")
    gid = m.group_id
    await env.post(f"/admin/users/{m.id}", data={"group_id": "9999", "enabled": "1", "csrf_token": t})
    assert q(env, User, username="mario").group_id == gid                       # not saved: it would mean "no limits"
    await env.post("/admin/users", data={"username": "ghost", "group_id": "4242", "csrf_token": t})
    assert q(env, User, username="ghost") is None
    for bad in ("9" * 30, "\u00b2", "-1", "1e3"):                               # overflow, unicode digit, sign, float
        r = await env.post(f"/admin/users/{m.id}", data={"enabled": "1", "group_id": str(gid), "jobs_limit": bad, "csrf_token": t})
        assert r.status_code == 303, bad                                         # a flash message, never a 500
    with env.sm() as s:
        assert s.query(PolicyRule).filter_by(subject_id=m.id, subject_type="user").count() == 0


async def test_root_redirects_to_the_portal(env):
    r = await env.get("/")
    assert r.status_code == 307 and r.headers["location"] == "/admin/"


async def test_admin_without_trailing_slash_and_unknown_paths(env):
    r = await env.get("/admin")
    assert r.status_code == 307 and r.headers["location"] == "/admin/"      # was 405
    assert (await env.get("/something")).status_code == 404                  # was 405
    assert (await env.get("/favicon.ico")).status_code == 301                # redirects to the icon (was 405, then 404)
    assert (await env.get("/admin/jobs")).status_code == 303                # real pages unaffected


async def test_same_typo_repeated_does_not_lock_the_admin_login(env):
    for _ in range(8):
        await login(env, "boss", "typo")
    assert (await login(env)).headers["location"] == "/admin/"


async def test_icon_is_public_and_linked_from_the_pages(portal):
    login = await portal.get("/login")
    assert 'href="/admin/static/icon.svg?v=' in login.text and 'href="/admin/static/favicon.ico?v=' in login.text
    for path, kind in (("/admin/static/icon.svg", "image/svg+xml"), ("/admin/static/icon-dark.svg", "image/svg+xml"), ("/admin/static/icon-light.svg", "image/svg+xml"), ("/admin/static/favicon.ico", "image/"),
                       ("/admin/static/apple-touch-icon.png", "image/png")):
        r = await portal.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind), path
    fav = await portal.get("/favicon.ico")
    assert fav.status_code == 301 and fav.headers["location"] == "/admin/static/favicon.ico"
    assert (await portal.get("/admin/static/../../../etc/passwd")).status_code in (404, 400)

