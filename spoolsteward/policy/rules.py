"""Create/replace/remove limits for a user or group (shared by CLI and admin UI)."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from spoolsteward.i18n import _
from spoolsteward.db.models import Metric, Period, PolicyRule, Resource, SubjectType


MAX_LIMIT = 1_000_000


def set_limit(
    session: Session, subject_type: SubjectType, subject_id: int, metric: Metric,
    limit: int | None, period: Period = Period.weekly,
) -> None:
    """limit=None removes the rule (falls back to the group / unlimited)."""
    if limit is not None and not 0 <= limit <= MAX_LIMIT:
        raise ValueError(_("limit must be between 0 and %(max)s", max=MAX_LIMIT))
    session.execute(delete(PolicyRule).where(
        PolicyRule.subject_type == subject_type, PolicyRule.subject_id == subject_id,
        PolicyRule.resource == Resource.print, PolicyRule.metric == metric,
    ))
    if limit is not None:
        session.add(PolicyRule(subject_type=subject_type, subject_id=subject_id, resource=Resource.print,
                               metric=metric, limit=limit, period=period))


def get_limits(session: Session, subject_type: SubjectType, subject_id: int) -> dict[Metric, PolicyRule]:
    rows = session.scalars(select(PolicyRule).where(
        PolicyRule.subject_type == subject_type, PolicyRule.subject_id == subject_id,
        PolicyRule.resource == Resource.print, PolicyRule.enabled,
    )).all()
    return {r.metric: r for r in rows}
