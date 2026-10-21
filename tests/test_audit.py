import threading
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from spoolsteward.db.models import Base, Group, JobLog, JobState, PolicyRule, SubjectType, User
from spoolsteward.policy import audit

TZ = ZoneInfo("Europe/Rome")


@pytest.fixture
def sm():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sm = sessionmaker(engine, expire_on_commit=False)
    with sm() as s:
        g = Group(name="g")
        s.add(g); s.flush()
        s.add(User(username="u", group_id=g.id))
        s.add(PolicyRule(subject_type=SubjectType.group, subject_id=g.id, limit=3))
        s.commit()
    return sm


def _user(sm):
    with sm() as s:
        return s.scalars(select(User)).one()


def test_reserve_denies_over_limit_and_logs_reason(sm):
    u = _user(sm)
    with sm() as s:
        results = [audit.reserve(s, u, "p", f"f{i}.pdf", {}, TZ)[0].allowed for i in range(5)]
        assert results == [True, True, True, False, False]
        denied = s.scalars(select(JobLog).where(JobLog.state == JobState.denied)).all()
        assert len(denied) == 2 and "3/3" in denied[0].denied_reason


def test_concurrent_reserves_never_overshoot(sm):
    u = _user(sm)
    allowed = []

    def worker():
        with sm() as s:
            allowed.append(audit.reserve(s, u, "p", "x", {}, TZ)[0].allowed)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert sum(allowed) == 3


def test_failed_job_frees_the_slot(sm):
    u = _user(sm)
    with sm() as s:
        ids = [audit.reserve(s, u, "p", "x", {}, TZ)[1] for _ in range(3)]
        assert not audit.reserve(s, u, "p", "x", {}, TZ)[0].allowed
        audit.finalize(s, ids[0], ok=False, cups_job_id=None, reason="CUPS status 0x0404")
        assert audit.reserve(s, u, "p", "x", {}, TZ)[0].allowed


def test_attach_document_fills_untitled(sm):
    u = _user(sm)
    with sm() as s:
        _, i = audit.reserve(s, u, "p", "untitled", {}, TZ)
        audit.finalize(s, i, ok=True, cups_job_id=42)
        audit.attach_document(s, u.id, 42, "contract.pdf", "application/pdf")
        row = s.get(JobLog, i)
        assert row.title == "contract.pdf" and row.options_json["document_name"] == "contract.pdf"


def test_independent_connections_never_overshoot(tmp_path):
    """Separate engines = what separate worker processes see. Needs the DB-level lock."""
    from spoolsteward.db.session import make_engine

    url = f"sqlite:///{tmp_path}/q.db"
    eng = make_engine(url)
    Base.metadata.create_all(eng)
    with sessionmaker(eng, expire_on_commit=False)() as s:
        g = Group(name="g"); s.add(g); s.flush()
        u = User(username="u", group_id=g.id); s.add(u)
        s.add(PolicyRule(subject_type=SubjectType.group, subject_id=g.id, limit=3)); s.commit()
        uid = u.id
    allowed = []

    def worker():
        e = make_engine(url)  # own engine + connection, like a separate process
        with sessionmaker(e, expire_on_commit=False)() as s:
            user = s.get(User, uid)
            allowed.append(audit.reserve(s, user, "p", "x", {}, TZ)[0].allowed)
        e.dispose()

    audit._submit_lock = type("NoLock", (), {"__enter__": lambda s: None, "__exit__": lambda s, *a: None})()
    try:
        threads = [threading.Thread(target=worker) for _ in range(16)]
        [t.start() for t in threads]; [t.join() for t in threads]
    finally:
        audit._submit_lock = threading.Lock()
    assert len(allowed) == 16 and sum(allowed) == 3
