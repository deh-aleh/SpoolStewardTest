"""An administrator can cancel a job that is still waiting in a queue - as the job's owner, so CUPS' own
@OWNER policy accepts it and the gateway needs no extra privilege."""

import base64

import httpx
import pytest
from sqlalchemy import select

from spoolsteward.auth import keys
from spoolsteward.config import settings
from spoolsteward.db.models import AdminAudit, JobLog, JobState, User
from spoolsteward.ipp import codec as c
from tests.conftest import CUPS_TEST_URL, csrf_of_page, make_user, portal_login, requires_cups

GOOD = "correct horse battery"
pytestmark = requires_cups


@pytest.fixture
async def world(portal, monkeypatch):
    monkeypatch.setattr(settings, "ipp_auth", "keys")
    portal.app.state.upstream = httpx.AsyncClient(base_url=CUPS_TEST_URL, timeout=10)
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    make_user(portal.sm, "ann", password=GOOD)
    with portal.sm() as s:
        _, portal.key = keys.create_key(s, s.scalars(select(User).where(User.username == "ann")).one(), "t", 30)
    yield portal
    await portal.app.state.upstream.aclose()


def basic(user, secret):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode(), "Content-Type": "application/ipp"}


async def ipp(cl, req, user="ann", secret=None):
    r = await cl.post("/printers/virtual", content=c.encode(req), headers=basic(user, secret or cl.key))
    return c.decode(r.content)


async def create_waiting_job(cl):
    """Create-Job without a document: CUPS keeps it pending-held, i.e. waiting in the queue."""
    out = await ipp(cl, c.make_request(c.CREATE_JOB, 1, printer_uri="ipp://gw.test/printers/virtual", job_name="hold.pdf"))
    assert out.code < 0x0100
    return out.get(c.TAG_JOB, "job-id")


def job_state(cl_out):
    return cl_out.get(c.TAG_JOB, "job-state")


async def cups_state(cl, job_id):
    q = c.make_request(0x0009, 2, printer_uri="ipp://gw.test/printers/virtual")
    q.group(c.TAG_OPERATION).attrs.append(c.Attr("job-id", [c.Val(c.TAG_INTEGER, job_id)]))
    return job_state(await ipp(cl, q))


def log_id(cl):
    with cl.sm() as s:
        return s.scalars(select(JobLog)).one().id


async def test_admin_cancels_a_waiting_job_and_it_is_recorded(world):
    job_id = await create_waiting_job(world)
    assert await cups_state(world, job_id) in (3, 4)                         # pending / pending-held
    boss = httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app, client=("10.0.0.9", 1)), base_url="https://gw.test")
    async with boss:
        await portal_login(boss, "boss", GOOD)
        token = await csrf_of_page(boss, "/admin/users")
        assert "/cancel" in (await boss.get("/admin/jobs")).text             # the button is offered for this row
        r = await boss.post(f"/admin/jobs/{log_id(world)}/cancel", data={"csrf_token": token})
        assert r.status_code == 303
    assert await cups_state(world, job_id) == 7                              # canceled in CUPS
    with world.sm() as s:
        a = s.scalars(select(AdminAudit).where(AdminAudit.action == "job.cancel")).one()
        assert a.actor_name == "boss" and a.detail_json["owner"] == "ann" and a.detail_json["cups_job_id"] == job_id
        assert s.scalars(select(JobLog)).one().state is JobState.accepted    # the audit row is left to the reconciler


async def test_only_administrators_can_cancel_and_only_waiting_jobs(world):
    job_id = await create_waiting_job(world)
    await portal_login(world, "ann", GOOD)
    token = await csrf_of_page(world, "/me")
    assert (await world.post(f"/admin/jobs/{log_id(world)}/cancel", data={"csrf_token": token})).status_code == 403
    assert await cups_state(world, job_id) in (3, 4)                         # untouched
    boss = httpx.AsyncClient(transport=httpx.ASGITransport(app=world.app, client=("10.0.0.9", 1)), base_url="https://gw.test")
    async with boss:
        await portal_login(boss, "boss", GOOD)
        token = await csrf_of_page(boss, "/admin/users")
        with world.sm() as s:
            s.query(JobLog).update({"state": JobState.completed}); s.commit()
        r = await boss.post(f"/admin/jobs/{log_id(world)}/cancel", data={"csrf_token": token})
        assert r.status_code == 303
        assert "not waiting" in (await boss.get("/admin/jobs")).text         # a finished job is refused with a message
        assert (await boss.post("/admin/jobs/9999/cancel", data={"csrf_token": token})).status_code == 404
    assert await cups_state(world, job_id) in (3, 4)


async def test_users_land_on_their_own_page_not_on_an_error(world):
    await portal_login(world, "ann", GOOD)
    r = await world.get("/admin/")
    assert r.status_code == 303 and r.headers["location"] == "/me"
