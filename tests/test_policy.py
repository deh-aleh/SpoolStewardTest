from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import (
    Base, Group, JobKind, JobLog, JobState, Metric, Period, PolicyRule, Resource, SubjectType, User,
)
from app.policy.engine import check_job, window_start

TZ = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)  # Wednesday


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _setup(session, limit=2, user_limit=None):
    g = Group(name="staff")
    session.add(g)
    session.flush()
    u = User(username="mario", group_id=g.id)
    session.add(u)
    session.flush()
    session.add(PolicyRule(subject_type=SubjectType.group, subject_id=g.id, limit=limit))
    if user_limit is not None:
        session.add(PolicyRule(subject_type=SubjectType.user, subject_id=u.id, limit=user_limit))
    session.commit()
    return u


def _log(session, user, ts=NOW, state=JobState.accepted):
    session.add(JobLog(user_id=user.id, ts=ts, kind=JobKind.print, state=state, title="a.pdf"))
    session.commit()


def test_window_start_monday():
    assert window_start(Period.weekly, NOW, TZ) == datetime(2026, 10, 5, tzinfo=TZ)


def test_monthly_window_starts_first_of_local_month():
    assert window_start(Period.monthly, NOW, TZ) == datetime(2026, 10, 1, tzinfo=TZ)
    late = datetime(2026, 9, 30, 22, 30, tzinfo=UTC)  # already Oct 1st 00:30 in Rome
    assert window_start(Period.monthly, late, TZ) == datetime(2026, 10, 1, tzinfo=TZ)


def test_pages_limit_sums_pages_and_is_soft(session):
    u = _setup(session, limit=100)
    session.add(PolicyRule(subject_type=SubjectType.group, subject_id=u.group_id, metric=Metric.pages, limit=10))
    session.add_all([JobLog(user_id=u.id, ts=NOW, kind=JobKind.print, state=JobState.completed, pages=6),
                     JobLog(user_id=u.id, ts=NOW, kind=JobKind.print, state=JobState.completed, pages=3)])
    session.commit()
    assert check_job(session, u, Resource.print, NOW, TZ).allowed  # 9/10
    _log(session, u, state=JobState.completed)
    session.query(JobLog).filter(JobLog.pages.is_(None)).update({"pages": 4})
    session.commit()
    d = check_job(session, u, Resource.print, NOW, TZ)  # 13/10
    assert not d.allowed and "page limit reached (13/10)" in d.reason


def test_canceled_and_failed_do_not_count(session):
    u = _setup(session, limit=1)
    _log(session, u, state=JobState.canceled)
    _log(session, u, state=JobState.failed)
    assert check_job(session, u, Resource.print, NOW, TZ).allowed


def test_user_rule_overrides_only_its_own_metric(session):
    u = _setup(session, limit=1)
    session.add(PolicyRule(subject_type=SubjectType.group, subject_id=u.group_id, metric=Metric.pages, limit=5))
    session.add(PolicyRule(subject_type=SubjectType.user, subject_id=u.id, metric=Metric.jobs, limit=9))
    session.commit()
    from app.policy.engine import effective_rules
    got = {r.metric: r.limit for r in effective_rules(session, u, Resource.print)}
    assert got == {Metric.jobs: 9, Metric.pages: 5}


def test_under_limit_allowed(session):
    u = _setup(session)
    _log(session, u)
    assert check_job(session, u, Resource.print, NOW, TZ).allowed


def test_at_limit_denied(session):
    u = _setup(session)
    _log(session, u)
    _log(session, u)
    d = check_job(session, u, Resource.print, NOW, TZ)
    assert not d.allowed and "2/2" in d.reason


def test_denied_and_old_jobs_not_counted(session):
    u = _setup(session)
    _log(session, u, state=JobState.denied)
    _log(session, u, ts=datetime(2026, 9, 1, tzinfo=UTC))
    _log(session, u)
    assert check_job(session, u, Resource.print, NOW, TZ).allowed


def test_user_rule_overrides_group(session):
    u = _setup(session, limit=1, user_limit=5)
    _log(session, u)
    assert check_job(session, u, Resource.print, NOW, TZ).allowed


def test_disabled_user_denied(session):
    u = _setup(session)
    u.enabled = False
    assert not check_job(session, u, Resource.print, NOW, TZ).allowed


def test_pages_in_flight_count_as_one_page_each(session):
    """A burst submitted before CUPS reports page counts must not all see '0 pages used'."""
    u = _setup(session, limit=100)
    session.add(PolicyRule(subject_type=SubjectType.group, subject_id=u.group_id, metric=Metric.pages, limit=3))
    session.commit()
    for _ in range(3):
        _log(session, u)                      # accepted, pages unknown yet
    d = check_job(session, u, Resource.print, NOW, TZ)
    assert not d.allowed and "page limit reached (3/3)" in d.reason
