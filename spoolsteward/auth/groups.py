"""The two groups every installation has: "default" (where everybody starts) and "administrator"."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from spoolsteward.config import settings
from spoolsteward.auth.users import is_emergency
from spoolsteward.db.models import Group, PermissionRule, PolicyRule, SubjectType, User

ADMIN_GROUP = "administrator"


def system_group_names() -> set[str]:
    return {settings.default_group, ADMIN_GROUP}


def _get_or_create(db: Session, name: str) -> Group:
    g = db.scalars(select(Group).where(Group.name == name)).first()
    if g is None:
        g = Group(name=name)
        db.add(g)
        db.flush()
    return g


def set_admin(db: Session, user: User, flag: bool) -> None:
    """Promote/demote and move the account: administrators live in "administrator", demoted ones go back to "default"."""
    user.is_admin = bool(flag)
    if is_emergency(user):  # the built-in administrator belongs to no group: nothing limits it
        user.group_id = None
    elif flag:
        user.group_id = _get_or_create(db, ADMIN_GROUP).id
    else:
        admin_group = db.scalars(select(Group).where(Group.name == ADMIN_GROUP)).first()
        if admin_group is not None and user.group_id == admin_group.id:
            user.group_id = _get_or_create(db, settings.default_group).id


def ensure_system_groups(sm) -> None:
    """At start: both groups exist; every administrator is in "administrator", nobody else is. The only exception is the
    emergency administrator: it has no group and no limits of its own (rows left from an older version are removed)."""
    with sm() as db:
        default, admins = _get_or_create(db, settings.default_group), _get_or_create(db, ADMIN_GROUP)
        for u in db.scalars(select(User)):
            if is_emergency(u):
                u.group_id = None
                for model in (PolicyRule, PermissionRule):
                    db.execute(delete(model).where(model.subject_type == SubjectType.user, model.subject_id == u.id))
            elif u.is_admin and u.group_id != admins.id:
                u.group_id = admins.id
            elif not u.is_admin and u.group_id == admins.id:
                u.group_id = default.id
        db.commit()
