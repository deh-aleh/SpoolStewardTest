import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, JobKind, JobLog, JobState, User
from app.ipp import codec as c
from app.policy.reconcile import apply_updates, reconcile_once, release_orphans
from tests.conftest import CUPS_TEST_URL, requires_cups

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def sm():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _row(s, job_id, state=JobState.accepted, ts=NOW):
    r = JobLog(ts=ts, kind=JobKind.print, printer="p", cups_job_id=job_id, state=state, title="t")
    s.add(r); s.commit()
    return r


def test_apply_updates_matrix(sm):
    with sm() as s:
        rows = [_row(s, i) for i in range(1, 8)] + [_row(s, 99, ts=NOW - timedelta(days=2)), _row(s, 98)]
        cups = {"p": {
            1: (9, 3),   # completed, 3 pages
            2: (9, 0),   # completed, printer reports nothing
            3: (7, 0),   # canceled before printing -> slot freed
            4: (7, 2),   # canceled after 2 pages -> counts
            5: (8, 0),   # aborted -> failed
            6: (4, 0),   # pending-held (Create-Job waiting for a document) -> untouched
            7: (5, 0),   # processing -> untouched
        }}
        apply_updates(s, rows, cups, NOW)
        got = {r.cups_job_id: (r.state.value, r.pages) for r in rows}
    assert got == {
        1: ("completed", 3), 2: ("completed", None), 3: ("canceled", None), 4: ("completed", 2),
        5: ("failed", None), 6: ("accepted", None), 7: ("accepted", None),
        99: ("completed", None),  # vanished from CUPS history after > 1 day
        98: ("accepted", None),   # vanished but recent: wait
    }


def test_cups_unreachable_changes_nothing(sm):
    with sm() as s:
        r = _row(s, 1, ts=NOW - timedelta(days=3))
        assert apply_updates(s, [r], {"p": None}, NOW) == 0
        assert r.state is JobState.accepted


@requires_cups
async def test_reconcile_against_real_cups(sm):
    up = httpx.AsyncClient(base_url=CUPS_TEST_URL, timeout=10)
    try:
        # one job that prints, one Create-Job that is canceled without a document
        pj = c.make_request(c.PRINT_JOB, 1, printer_uri="ipp://localhost/printers/virtual", requesting_user_name="t",
                            job_name="rec.txt", document_format="text/plain")
        pj.data = b"x\n"
        r = c.decode((await up.post("/printers/virtual", content=c.encode(pj), headers={"Content-Type": "application/ipp", "Host": "localhost"})).content)
        printed = r.get(c.TAG_JOB, "job-id")
        cj = c.make_request(c.CREATE_JOB, 2, printer_uri="ipp://localhost/printers/virtual", requesting_user_name="t")
        r = c.decode((await up.post("/printers/virtual", content=c.encode(cj), headers={"Content-Type": "application/ipp", "Host": "localhost"})).content)
        held = r.get(c.TAG_JOB, "job-id")
        cancel = c.make_request(0x0008, 3, printer_uri="ipp://localhost/printers/virtual", requesting_user_name="t")
        cancel.group(c.TAG_OPERATION).attrs.append(c.Attr("job-id", [c.Val(c.TAG_INTEGER, held)]))
        await up.post("/printers/virtual", content=c.encode(cancel), headers={"Content-Type": "application/ipp", "Host": "localhost"})
        with sm() as s:
            _row(s, printed, ts=datetime.now(UTC)).printer = "virtual"
            _row(s, held, ts=datetime.now(UTC))
            s.query(JobLog).update({"printer": "virtual"}); s.commit()
        await asyncio.sleep(3)
        assert await reconcile_once(sm, up) == 2
        with sm() as s:
            states = {r.cups_job_id: r.state for r in s.scalars(select(JobLog))}
        assert states == {printed: JobState.completed, held: JobState.canceled}
    finally:
        await up.aclose()


def test_orphaned_reservations_are_released_after_an_hour(sm):
    with sm() as s:
        fresh = JobLog(ts=NOW - timedelta(minutes=5), kind=JobKind.print, printer="p", state=JobState.accepted, title="up")
        stale = JobLog(ts=NOW - timedelta(hours=2), kind=JobKind.print, printer="p", state=JobState.accepted, title="dead")
        with_id = JobLog(ts=NOW - timedelta(hours=2), kind=JobKind.print, printer="p", cups_job_id=7, state=JobState.accepted, title="ok")
        s.add_all([fresh, stale, with_id]); s.commit()
    assert release_orphans(sm, NOW) == 1
    with sm() as s:
        got = {r.title: r.state for r in s.scalars(select(JobLog))}
    assert got == {"up": JobState.accepted, "dead": JobState.failed, "ok": JobState.accepted}


def test_a_job_number_reused_by_a_newer_cups_job_is_not_taken_for_ours(sm):
    """CUPS restarted its numbering (volume recreated): job 5 of today is not the job 5 we reserved last week."""
    from datetime import timedelta
    with sm() as s:
        user = User(provider="local", username="reuse")
        s.add(user)
        s.flush()
        old = JobLog(ts=NOW - timedelta(days=3), user_id=user.id, user_name="reuse", kind=JobKind.print, printer="p", cups_job_id=5, state=JobState.accepted)
        mine = JobLog(ts=NOW - timedelta(minutes=2), user_id=user.id, user_name="reuse", kind=JobKind.print, printer="p", cups_job_id=6, state=JobState.accepted)
        s.add_all([old, mine])
        s.flush()
        born = int((NOW - timedelta(minutes=2)).timestamp())
        cups = {"p": {5: (9, 7, born), 6: (9, 2, born)}}   # job 5 in CUPS is a new one, made 2 minutes ago
        apply_updates(s, [old, mine], cups, NOW)
        assert old.pages is None and old.state == JobState.completed   # not given the 7 pages of someone else's job (gone for > 1 day)
        assert mine.pages == 2
