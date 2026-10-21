"""Administrative audit trail: who changed what, separate from the print audit (job_log)."""

from sqlalchemy.orm import Session

from spoolsteward.db.models import AdminAudit, User


def record(session: Session, actor: User | None, action: str, target_type: str | None = None,
           target_id: int | None = None, *, actor_name: str | None = None, **detail) -> AdminAudit:
    """Adds the entry to the session; the caller commits it together with the change it describes."""
    row = AdminAudit(
        actor_id=actor.id if actor is not None else None,
        actor_name=actor.username if actor is not None else (actor_name or "cli"),
        action=action, target_type=target_type, target_id=target_id, detail_json=detail or None,
    )
    session.add(row)
    return row
