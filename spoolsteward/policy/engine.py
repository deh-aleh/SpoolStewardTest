from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from spoolsteward.db.models import (
    JobKind,
    JobLog,
    JobState,
    Metric,
    Period,
    PolicyRule,
    QuotaGrant,
    Resource,
    SubjectType,
    User,
)
from spoolsteward.auth.users import is_emergency
from spoolsteward.i18n import N_, _

# states that consume quota; failed/canceled/denied free it
COUNTED = (JobState.accepted, JobState.completed, JobState.held)


@dataclass
class Decision:
    """`reason` is English and goes to the audit and to IPP clients; `text()` is the same said in the language of the
    request (the portal shows that one)."""

    allowed: bool
    reason: str | None = None
    localized: Callable[[], str] | None = field(default=None, compare=False, repr=False)
    quota: dict | None = field(default=None, compare=False, repr=False)  # set when a quota limit is the reason (for the user's bell)

    def text(self) -> str | None:
        if self.localized is not None:
            return self.localized()
        return _(self.reason) if self.reason else self.reason


def _local_window_start(period: Period, now: datetime, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period is Period.weekly:
        return day - timedelta(days=day.weekday())
    if period is Period.monthly:
        return day.replace(day=1)
    raise ValueError(period)


def window_start(period: Period, now: datetime, tz: ZoneInfo) -> datetime:
    """Start of the current period (calendar week Mon-Sun, or calendar month), in UTC.

    Boundaries follow the local calendar in `tz`; the result is UTC because job_log
    timestamps are stored as UTC (SQLite keeps no tz info, so both sides must match).
    """
    return _local_window_start(period, now, tz).astimezone(UTC)


def window_end(period: Period, now: datetime, tz: ZoneInfo) -> datetime:
    """Start of the NEXT period, in UTC: the first instant a temporary grant is no longer valid."""
    start = _local_window_start(period, now, tz)
    if period is Period.weekly:
        nxt = start + timedelta(days=7)  # wall-clock arithmetic: still Monday 00:00 across a DST change
    else:
        nxt = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
    return nxt.astimezone(UTC)


def effective_rules(session: Session, user: User, resource: Resource) -> list[PolicyRule]:
    """One rule per metric. A user rule overrides the group rule for the same metric. The emergency administrator has none."""
    if is_emergency(user):
        return []
    rules: dict[Metric, PolicyRule] = {}
    subjects = []
    if user.group_id is not None:
        subjects.append((SubjectType.group, user.group_id))
    subjects.append((SubjectType.user, user.id))  # applied last -> wins
    for stype, sid in subjects:
        for r in session.scalars(
            select(PolicyRule).where(
                PolicyRule.enabled, PolicyRule.resource == resource,
                PolicyRule.subject_type == stype, PolicyRule.subject_id == sid,
            ).order_by(PolicyRule.id)
        ):
            rules[r.metric] = r
    return list(rules.values())


def usage(session: Session, user: User, rule: PolicyRule, now: datetime, tz: ZoneInfo) -> int:
    start = window_start(rule.period, now, tz)
    if rule.metric is Metric.pages:
        # a job whose page count CUPS has not reported yet (still queued/printing, or a printer that
        # never reports) is worth at least one page. Without this a burst of jobs submitted before the
        # reconciler runs would all see "0 pages used" and slip through.
        agg = func.coalesce(func.sum(case((JobLog.pages.is_(None), 1), else_=JobLog.pages)), 0)
    else:
        agg = func.count(JobLog.id)
    return session.scalar(
        select(agg).where(
            JobLog.user_id == user.id,
            JobLog.kind == JobKind(rule.resource.value),
            JobLog.state.in_(COUNTED),
            JobLog.ts >= start,
        )
    ) or 0


def active_grants(session: Session, user: User, metric: Metric, now: datetime) -> int:
    """Extra units granted to the user for this metric right now (temporary ones count until their window ends)."""
    return session.scalar(
        select(func.coalesce(func.sum(QuotaGrant.amount), 0)).where(
            QuotaGrant.user_id == user.id, QuotaGrant.metric == metric,
            QuotaGrant.revoked_at.is_(None), QuotaGrant.valid_from <= now,
            (QuotaGrant.valid_until.is_(None)) | (QuotaGrant.valid_until > now),
        )
    ) or 0


def active_grant_list(session: Session, user: User, metric: Metric, now: datetime) -> list[QuotaGrant]:
    """The grants behind `active_grants`, one by one (to show amount and expiry)."""
    return list(session.scalars(
        select(QuotaGrant).where(
            QuotaGrant.user_id == user.id, QuotaGrant.metric == metric,
            QuotaGrant.revoked_at.is_(None), QuotaGrant.valid_from <= now,
            (QuotaGrant.valid_until.is_(None)) | (QuotaGrant.valid_until > now),
        ).order_by(QuotaGrant.id)))


def effective_limit(session: Session, user: User, rule: PolicyRule, now: datetime) -> int:
    return rule.limit + active_grants(session, user, rule.metric, now)


def check_job(session: Session, user: User, resource: Resource, now: datetime, tz: ZoneInfo,
              pages: int | None = None) -> Decision:
    """Deny when any applicable limit is already used up.

    `pages` is the page count of the document when the gateway could count it (PDF, images...): the pages limit is
    then hard, a 200-page file is refused when it does not fit. Without a count the pages limit is soft: the job
    that crosses the line is allowed (its real pages are recorded once CUPS has printed it, blocking the next one).
    """
    if not user.enabled:
        return Decision(False, N_("user disabled"))
    for rule in effective_rules(session, user, resource):
        used = usage(session, user, rule, now, tz)
        limit = effective_limit(session, user, rule, now)
        if rule.metric is Metric.pages and pages and used + pages > limit:
            return Decision(False, f"{rule.period.value} page limit would be exceeded "
                                   f"(used {used}, this document {pages}, limit {limit})",
                            lambda: _("%(period)s page limit would be exceeded (used %(used)s, this document %(pages)s, limit %(limit)s)",
                                      period=_(rule.period.value), used=used, pages=pages, limit=limit),
                            quota={"period": rule.period.value, "metric": rule.metric.value, "used": used, "limit": limit, "pages": pages})
        if used >= limit:
            noun = "page" if rule.metric is Metric.pages else "job"
            return Decision(False, f"{rule.period.value} {noun} limit reached ({used}/{limit})",
                            lambda: _("%(period)s %(noun)s limit reached (%(used)s/%(limit)s)", period=_(rule.period.value),
                                      noun=_(noun), used=used, limit=limit),
                            quota={"period": rule.period.value, "metric": rule.metric.value, "used": used, "limit": limit})
    return Decision(True)
