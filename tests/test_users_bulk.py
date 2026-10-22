"""The user list computes every user's limits with a fixed number of queries; the result must equal the per-user code."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from spoolsteward.admin import accounts
from spoolsteward.admin.accounts import _limit_view
from spoolsteward.admin.common import localtime
from spoolsteward.config import settings
from spoolsteward.db.models import (Base, Group, JobKind, JobLog, JobState, Metric, Period, PolicyRule, QuotaGrant,
                                    RequestScope, Resource, SubjectType, User)
from spoolsteward.policy.bulk import limit_views
from spoolsteward.policy.engine import window_start
from spoolsteward.timeutil import local_tz
from tests.conftest import csrf_of_page, make_user  # noqa: F401

NOW = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)  # a Wednesday, mid month


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bulk.db'}")
    Base.metadata.create_all(engine)
    sm = sessionmaker(engine, expire_on_commit=False)
    sm.engine = engine
    return sm


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    """`_usage_lines` of the old code reads the clock itself."""
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(accounts, "datetime", Frozen)


def populate(sm, n: int) -> None:
    """n users in 4 groups with every kind of rule, grant and job."""
    tz = local_tz()
    week, month = window_start(Period.weekly, NOW, tz), window_start(Period.monthly, NOW, tz)
    stamps = [NOW - timedelta(hours=1), week + timedelta(minutes=1), week - timedelta(hours=1),
              month + timedelta(minutes=1), month - timedelta(hours=1)]
    with sm() as s:
        groups = [Group(name=f"g{i}") for i in range(4)]
        s.add_all(groups)
        s.flush()
        s.add_all([  # g0: weekly jobs+pages; g1: monthly jobs; g2: monthly pages, disabled jobs; g3: nothing
            PolicyRule(subject_type=SubjectType.group, subject_id=groups[0].id, metric=Metric.jobs, limit=10, period=Period.weekly),
            PolicyRule(subject_type=SubjectType.group, subject_id=groups[0].id, metric=Metric.pages, limit=50, period=Period.weekly),
            PolicyRule(subject_type=SubjectType.group, subject_id=groups[1].id, metric=Metric.jobs, limit=20, period=Period.monthly),
            PolicyRule(subject_type=SubjectType.group, subject_id=groups[2].id, metric=Metric.pages, limit=80, period=Period.monthly),
            PolicyRule(subject_type=SubjectType.group, subject_id=groups[2].id, metric=Metric.jobs, limit=5, enabled=False),
            PolicyRule(subject_type=SubjectType.group, subject_id=groups[1].id, resource=Resource.scan, metric=Metric.jobs, limit=1),
        ])
        users = [User(username=settings.bootstrap_admin_username, provider="local", is_admin=True, group_id=groups[0].id)]
        users += [User(username=f"u{i:03d}", provider="local", group_id=groups[i % 4].id if i % 9 else None,
                       enabled=i % 7 != 0) for i in range(n - 1)]
        s.add_all(users)
        s.flush()
        for i, u in enumerate(users):
            if i % 5 == 1:  # own rule overrides the group's, with another period
                s.add(PolicyRule(subject_type=SubjectType.user, subject_id=u.id, metric=Metric.jobs, limit=3, period=Period.monthly))
            if i % 6 == 2:  # own rule on the other metric, no group rule needed
                s.add(PolicyRule(subject_type=SubjectType.user, subject_id=u.id, metric=Metric.pages, limit=30, period=Period.weekly))
            if i % 11 == 3 and i % 5 != 1:
                s.add(PolicyRule(subject_type=SubjectType.user, subject_id=u.id, metric=Metric.jobs, limit=1, enabled=False))
            metric = Metric.jobs if i % 2 else Metric.pages
            kw = dict(user_id=u.id, scope=RequestScope.temporary, valid_from=NOW - timedelta(days=1))
            if i % 3 == 0:
                s.add(QuotaGrant(metric=metric, amount=7, valid_until=NOW + timedelta(days=2), **kw))
            if i % 4 == 0:
                s.add(QuotaGrant(metric=metric, amount=4, valid_until=None, **kw))
            if i % 8 == 0:
                s.add(QuotaGrant(metric=metric, amount=100, valid_until=NOW - timedelta(hours=1), **kw))   # expired
                s.add(QuotaGrant(metric=metric, amount=200, valid_until=None, revoked_at=NOW, **kw))        # revoked
            for k, state in enumerate(JobState):
                for j, ts in enumerate(stamps):
                    if (i + k + j) % 3 == 0:
                        s.add(JobLog(user_id=u.id, ts=ts, state=state, kind=JobKind.print if (i + j) % 4 else JobKind.scan,
                                     pages=None if (i + k) % 5 == 0 else (i + j) % 6))
        s.commit()


def test_bulk_equals_per_user_view(db):
    populate(db, 48)
    with db() as s:
        users = s.query(User).order_by(User.username).all()
        views = limit_views(s, users, NOW, local_tz(), localtime)
        assert len(users) == 48 and set(views) == {u.id for u in users}
        for u in users:
            assert views[u.id] == _limit_view(s, u, NOW), u.username
        # the dataset really exercises the branches
        seen = {(v["jobs"]["inherited"], v["pages"]["inherited"], v["period"]) for v in views.values()}
        assert any(not line.startswith("0/") for v in views.values() for line in v["usage"])
        assert len(seen) > 4 and any(v["jobs"]["extra"] or v["pages"]["extra"] for v in views.values())
        assert any("temporary" in v["jobs"]["tip"] + v["pages"]["tip"] for v in views.values())
        admin = next(u for u in users if u.username == settings.bootstrap_admin_username)
        assert views[admin.id]["usage"] == [] and views[admin.id]["jobs"]["limit"] is None


def statements(sm) -> int:
    counter = []
    event.listen(sm.engine, "before_cursor_execute", lambda *a: counter.append(a[2]))
    with sm() as s:
        users = s.query(User).order_by(User.username).all()
        counter.clear()
        limit_views(s, users, NOW, local_tz(), localtime)
    return len(counter)


def test_query_count_is_fixed(tmp_path):
    counts = {}
    for n in (20, 200):
        engine = create_engine(f"sqlite:///{tmp_path / f'q{n}.db'}")
        Base.metadata.create_all(engine)
        sm = sessionmaker(engine, expire_on_commit=False)
        sm.engine = engine
        populate(sm, n)
        counts[n] = statements(sm)
    assert counts[20] == counts[200] <= 12


async def test_users_page(admin):
    populate(admin.sm, 12)
    r = await admin.get("/admin/users")
    assert r.status_code == 200
    with admin.sm() as s:
        users = s.query(User).all()
        views = {u.id: _limit_view(s, u, NOW) for u in users}
    for u in users:
        assert u.username in r.text
    lines = {line for v in views.values() for line in v["usage"]}
    assert lines and all(line.replace("&", "&amp;") in r.text for line in lines)
    assert any(v["jobs"]["extra"] for v in views.values()) and "+7" in r.text and "+4" in r.text
    assert "g1" in r.text and "limit inherited from the group" in r.text
