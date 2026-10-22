"""The limits column of the user list for MANY users at once, in a fixed number of queries (rules, grants, one usage
sum per period in use) instead of a dozen per user. Same result as the per-user functions in engine.py / rules.py."""

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from spoolsteward.auth.users import is_emergency
from spoolsteward.db.models import JobKind, JobLog, Metric, Period, PolicyRule, QuotaGrant, Resource, SubjectType, User
from spoolsteward.i18n import _
from spoolsteward.policy.engine import COUNTED, window_start


def _rules(db: Session) -> tuple[dict[int, dict[Metric, PolicyRule]], dict[int, dict[Metric, PolicyRule]]]:
    """Enabled print rules per group and per user. Unfiltered by id on purpose: no IN list that grows with the users."""
    by_group: dict[int, dict[Metric, PolicyRule]] = defaultdict(dict)
    by_user: dict[int, dict[Metric, PolicyRule]] = defaultdict(dict)
    for r in db.scalars(select(PolicyRule).where(PolicyRule.enabled, PolicyRule.resource == Resource.print).order_by(PolicyRule.id)):
        (by_group if r.subject_type is SubjectType.group else by_user)[r.subject_id][r.metric] = r
    return by_group, by_user


def _grants(db: Session, now: datetime) -> dict[tuple[int, Metric], list[QuotaGrant]]:
    """Active grants (as engine.active_grant_list) per (user, metric), oldest first."""
    out: dict[tuple[int, Metric], list[QuotaGrant]] = defaultdict(list)
    for g in db.scalars(select(QuotaGrant).where(
            QuotaGrant.revoked_at.is_(None), QuotaGrant.valid_from <= now,
            (QuotaGrant.valid_until.is_(None)) | (QuotaGrant.valid_until > now)).order_by(QuotaGrant.id)):
        out[g.user_id, g.metric].append(g)
    return out


def _usage(db: Session, period: Period, now: datetime, tz: ZoneInfo) -> dict[int, dict[Metric, int]]:
    """Jobs and pages counted in the current window, per user, summed by the database (same rules as engine.usage)."""
    pages = func.coalesce(func.sum(case((JobLog.pages.is_(None), 1), else_=JobLog.pages)), 0)  # unreported pages count 1
    out: dict[int, dict[Metric, int]] = {}
    for uid, n, p in db.execute(select(JobLog.user_id, func.count(JobLog.id), pages).where(
            JobLog.user_id.is_not(None), JobLog.kind == JobKind.print, JobLog.state.in_(COUNTED),
            JobLog.ts >= window_start(period, now, tz)).group_by(JobLog.user_id)):
        out[uid] = {Metric.jobs: n or 0, Metric.pages: p or 0}
    return out


def limit_views(db: Session, users: Sequence[User], now: datetime, tz: ZoneInfo,
                localtime: Callable[[datetime | None, str], str]) -> dict[int, dict]:
    """{user id: what admin.accounts._limit_view returns for that user}."""
    by_group, by_user = _rules(db)
    grants = _grants(db, now)
    used: dict[Period, dict[int, dict[Metric, int]]] = {}
    out = {}
    for u in users:
        # group rules first, the user's own override them (same order as engine.effective_rules); none for the emergency admin
        own = by_user.get(u.id, {})
        rules = {} if is_emergency(u) else {**by_group.get(u.group_id, {}), **own}
        lines = []
        view: dict = {"usage": lines}
        for m in (Metric.jobs, Metric.pages):
            r = rules.get(m)
            inherited = bool(r) and m not in own
            gl = grants.get((u.id, m), []) if r else []
            tip = ([_("limit inherited from the group")] if inherited else []) + [
                f"+{g.amount} · " + (_("temporary, expires on %(date)s", date=localtime(g.valid_until, '%Y-%m-%d %H:%M')) if g.valid_until else _("permanent"))
                for g in gl]
            view[m.value] = {
                "limit": r.limit if r else None, "inherited": inherited,
                "extra": sum(g.amount for g in gl), "period": r.period.value if r else None, "tip": "\n".join(tip),
            }
        for r in rules.values():  # the order effective_rules gives
            if r.period not in used:
                used[r.period] = _usage(db, r.period, now, tz)
            extra = sum(g.amount for g in grants.get((u.id, r.metric), []))
            lines.append(_("%(used)s/%(limit)s %(metric)s (%(period)s)", used=used[r.period].get(u.id, {}).get(r.metric, 0),
                           limit=r.limit + extra, metric=_(r.metric.value), period=_(r.period.value)))
        view["period"] = next((r.period.value for r in rules.values()), "weekly")
        out[u.id] = view
    return out
