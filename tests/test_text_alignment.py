"""A refusal carries its reason twice: `reason` in English (audit, IPP clients, comparisons) and a `localized` text for the
language of the person. Two spellings of one sentence can drift apart: these tests keep them the same in English, and fail
when someone adds a refusal that is not covered here."""

import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import i18n
from app.db.models import Metric, PolicyRule, Resource, SubjectType
from app.policy.engine import check_job
from app.policy.permissions import Perms, Window, decide
from tests.test_policy import NOW, TZ, _log, _setup, session  # noqa: F401  (session is a fixture)

APP = Path(__file__).resolve().parent.parent / "app"
WED_10 = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
BOTH = {"color": True, "duplex": True}
UTC_ZONE = ZoneInfo("UTC")


def assert_same_in_english(d):
    assert not d.allowed and d.reason
    with i18n.using("en"):
        assert d.text() == d.reason


def test_job_refusals_say_the_same_in_english(session):  # noqa: F811
    u = _setup(session, limit=2)
    _log(session, u)
    _log(session, u)
    assert_same_in_english(check_job(session, u, Resource.print, NOW, TZ))                    # jobs limit reached
    session.add(PolicyRule(subject_type=SubjectType.user, subject_id=u.id, metric=Metric.pages, limit=5))
    session.commit()
    assert_same_in_english(check_job(session, u, Resource.print, NOW, TZ, pages=50))          # pages would be exceeded
    u.enabled = False
    assert_same_in_english(check_job(session, u, Resource.print, NOW, TZ))                    # user disabled


@pytest.mark.parametrize("perms, attrs, mode", [
    (Perms(printers=frozenset({"office"})), {}, "adjust"),                                   # printer
    (Perms(window=Window(frozenset({0}), 480, 600)), {}, "adjust"),                          # hours (Wednesday: closed)
    (Perms(color=False), {"print-color-mode": "color"}, "refuse"),                           # colour
    (Perms(one_sided=False), {"sides": "one-sided"}, "refuse"),                              # one-sided
])
def test_permission_refusals_say_the_same_in_english(perms, attrs, mode):
    d = decide(perms, "hp", BOTH, attrs, mode, WED_10, UTC_ZONE)
    assert_same_in_english(d)


def test_every_refusal_site_is_covered_by_the_tests_above():
    """If this fails you added a refusal: add a case above, then raise the number here."""
    engine = (APP / "policy" / "engine.py").read_text()
    perms = (APP / "policy" / "permissions.py").read_text()
    assert len(re.findall(r"Decision\(False", engine)) == 3
    assert len(re.findall(r"\b_refusal\(", perms)) == 5   # the definition + printer, hours, colour, one-sided
