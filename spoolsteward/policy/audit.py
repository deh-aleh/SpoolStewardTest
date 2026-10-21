"""Job accounting: atomically reserve a quota slot, then finalize once CUPS has answered."""

import threading
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from spoolsteward.timeutil import now_utc
from spoolsteward.db.models import JobKind, JobLog, JobState, Resource, User
from spoolsteward.policy import control
from spoolsteward.policy.engine import Decision, check_job
from spoolsteward.policy.permissions import PermDecision

# check + insert must be one critical section or concurrent submissions overshoot the limit.
# The thread lock covers one process (and in-memory test DBs); _lock_quota covers many processes.
_submit_lock = threading.Lock()


def _lock_quota(session: Session, user_id: int) -> None:
    """Serialize quota decisions across processes, until the session commits (SQLite: one writer at a time)."""
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")  # blocks other reservers


def reserve(
    session: Session, user: User, printer: str | None, title: str | None, options: dict, tz: ZoneInfo,
    pages: int | None = None, kind: JobKind = JobKind.print, deny: PermDecision | None = None,
) -> tuple[Decision, int]:
    """Check the quota and record the attempt. Allowed attempts count immediately (state=accepted).
    `pages` = counted pages of the document (copies included), recorded at once so concurrent jobs see them.
    A blocked printer or the maintenance mode refuses before any quota is looked at (and is audited as denied).
    `deny`: a refusal already decided by the caller (the user's permissions, see spoolsteward.policy.permissions): it is recorded as
    denied with its reason and the quota is not looked at, so nothing is consumed. A blocked printer still comes first."""
    now = now_utc()
    with _submit_lock:
        _lock_quota(session, user.id)
        why = control.block_reason(session, user, printer)
        if why:
            decision = Decision(False, why)
        elif deny is not None and not deny.allowed:
            decision = Decision(False, deny.reason, deny.localized)
        else:
            decision = check_job(session, user, Resource(kind.value), now, tz, pages)
        if not control.titles_enabled(session):  # the administrator turned the names off: only counts and states are kept
            title, options = None, {k: v for k, v in (options or {}).items() if k != "document_name"}
        row = JobLog(
            ts=now, user_id=user.id, user_name=user.username, kind=kind, printer=printer, title=title,
            options_json=options or None, pages=pages if decision.allowed else None,
            state=JobState.accepted if decision.allowed else JobState.denied,
            denied_reason=None if decision.allowed else decision.reason,
        )
        session.add(row)
        session.commit()
    return decision, row.id


def finalize(session: Session, log_id: int, ok: bool, cups_job_id: int | None, reason: str | None = None) -> None:
    row = session.get(JobLog, log_id)
    if row is None:
        return
    if ok:
        row.cups_job_id = cups_job_id
    else:  # CUPS refused it: free the slot
        row.state = JobState.failed
        row.denied_reason = reason
    session.commit()


def attach_document(session: Session, user_id: int, cups_job_id: int, name: str | None, fmt: str | None,
                    pages: int | None = None) -> None:
    """Send-Document after Create-Job: the file name usually only shows up here."""
    row = session.scalars(
        select(JobLog).where(JobLog.user_id == user_id, JobLog.cups_job_id == cups_job_id)
        .order_by(JobLog.id.desc())
    ).first()
    if row is None:
        return
    opts = dict(row.options_json or {})
    if name and not control.titles_enabled(session):
        name = None
    if name:
        opts["document_name"] = name
        if not row.title or row.title == "untitled":
            row.title = name
    if fmt:
        opts["document_format"] = fmt
    if pages and not row.pages:
        row.pages = pages  # counted at Send-Document: the next job already sees them
    row.options_json = opts
    session.commit()
