from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Group, User


def find_user(session: Session, username: str) -> User | None:
    """Usernames are unique across providers, so the name alone identifies the person."""
    return session.scalars(select(User).where(User.username == username)).first()


def is_emergency(user: User) -> bool:
    """The built-in administrator (SPOOLSTEWARD_BOOTSTRAP_ADMIN_USERNAME, a local account): it can not be disabled, demoted or
    deleted, and permissions do not limit it, so a wrong setting can never lock everybody out."""
    return user.username == settings.bootstrap_admin_username and user.provider == "local"


def default_group_id(session: Session, name: str) -> int:
    group = session.scalars(select(Group).where(Group.name == name)).first()
    if group is None:
        group = Group(name=name)
        session.add(group)
        session.flush()
    return group.id


def resolve_user(session: Session, provider: str, username: str, auto_create: bool, default_group: str) -> User | None:
    """The row for `username` if it exists (whatever its provider: the caller decides if that is acceptable),
    else a new one for `provider` when auto_create is on."""
    user = find_user(session, username)
    if user is not None or not auto_create:
        return user
    try:
        user = User(provider=provider, username=username, group_id=default_group_id(session, default_group))
        session.add(user)
        session.commit()
    except IntegrityError:  # a concurrent first login won the race: use its row
        session.rollback()
        user = find_user(session, username)
    return user


def delete_user(session: Session, user: User) -> dict:
    """Remove an account and everything that only makes sense with it. The print history stays: job rows keep the
    user's name (user_name) and just lose the link to the deleted row, so the audit still says who printed what."""
    from sqlalchemy import delete, update

    from app.db.models import (AdminAudit, BackupTarget, JobLog, PermissionGrant, PermissionRule, PolicyRule, PrintKey, PrinterOrder,
                               Notice, QuotaGrant, QuotaRequest, SubjectType)

    uid, name = user.id, user.username
    session.execute(update(JobLog).where(JobLog.user_id == uid, JobLog.user_name.is_(None)).values(user_name=name))
    jobs = session.execute(update(JobLog).where(JobLog.user_id == uid).values(user_id=None)).rowcount
    session.execute(update(AdminAudit).where(AdminAudit.actor_id == uid).values(actor_id=None))
    session.execute(update(BackupTarget).where(BackupTarget.owner_id == uid).values(owner_id=None, owner_name=name))
    session.execute(update(QuotaRequest).where(QuotaRequest.decided_by == uid).values(decided_by=None))
    session.execute(update(QuotaGrant).where(QuotaGrant.granted_by == uid).values(granted_by=None))
    session.execute(update(QuotaGrant).where(QuotaGrant.revoked_by == uid).values(revoked_by=None))
    session.execute(update(PermissionGrant).where(PermissionGrant.granted_by == uid).values(granted_by=None))
    session.execute(update(PermissionGrant).where(PermissionGrant.revoked_by == uid).values(revoked_by=None))
    session.execute(delete(PrinterOrder).where(PrinterOrder.user_id == uid))
    keys = session.execute(delete(PrintKey).where(PrintKey.user_id == uid)).rowcount
    grants = session.execute(delete(QuotaGrant).where(QuotaGrant.user_id == uid)).rowcount
    requests = session.execute(delete(QuotaRequest).where(QuotaRequest.user_id == uid)).rowcount
    rules = session.execute(delete(PolicyRule).where(PolicyRule.subject_type == SubjectType.user, PolicyRule.subject_id == uid)).rowcount
    session.execute(delete(PermissionGrant).where(PermissionGrant.user_id == uid))
    session.execute(delete(PermissionRule).where(PermissionRule.subject_type == SubjectType.user, PermissionRule.subject_id == uid))
    session.execute(delete(Notice).where(Notice.user_id == uid))  # not a foreign key: nothing removes them otherwise
    session.delete(user)
    return {"username": name, "jobs_kept": jobs, "keys": keys, "grants": grants, "requests": requests, "rules": rules}
