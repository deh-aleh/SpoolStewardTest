"""Deleting an account must not leave rows that point at it. The schema test fails the day somebody adds a foreign key
to `user` without deciding what deleting the account does to it; the live test then proves what delete_user does."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from app.auth.users import delete_user
from app.db.models import (
    AdminAudit, Base, BackupTarget, JobKind, JobLog, JobState, Metric, PermissionGrant, PermissionRule, Period, PolicyRule,
    PrinterOrder, PrintKey, QuotaGrant, QuotaRequest, RequestKind, RequestScope, SubjectType, User,
)
from tests.conftest import make_user

# What delete_user does with each column that points at user.id. "deleted": the row goes with the account.
# "detached": history that must survive, the column becomes NULL (jobs and audit keep the name as text).
HANDLED = {
    ("job_log", "user_id"): "detached",
    ("admin_audit", "actor_id"): "detached",
    ("backup_target", "owner_id"): "detached",
    ("quota_request", "decided_by"): "detached",
    ("quota_grant", "granted_by"): "detached",
    ("quota_grant", "revoked_by"): "detached",
    ("permission_grant", "granted_by"): "detached",
    ("permission_grant", "revoked_by"): "detached",
    ("printer_order", "user_id"): "deleted",
    ("print_key", "user_id"): "deleted",
    ("quota_grant", "user_id"): "deleted",
    ("quota_request", "user_id"): "deleted",
    ("permission_grant", "user_id"): "deleted",
}
# References by subject_type/subject_id (no foreign key, so the schema scan can not see them); delete_user removes them.
SUBJECT_TABLES = ("policy_rule", "permission_rule")


def user_references():
    return {(t.name, fk.parent.name) for t in Base.metadata.sorted_tables for fk in t.foreign_keys
            if fk.column.table.name == "user"}


def test_every_column_pointing_at_user_is_dealt_with_by_delete_user():
    found = user_references()
    missing = sorted(found - set(HANDLED))
    assert not missing, (f"{missing[0][0]}.{missing[0][1]} points at user.id but delete_user() does not handle it "
                         f"(all unhandled: {missing}): delete or detach it there, then list it in HANDLED")
    assert not sorted(set(HANDLED) - found), "HANDLED lists a column that no longer references user"


NOW = datetime.now(UTC)


def rows_for(victim, other):
    """One row in every referencing table that points at `victim` through exactly the given column; the other
    columns point at `other`, so a row can only disappear for the right reason."""
    k = "k" + str(victim)
    return {
        ("job_log", "user_id"): JobLog(user_id=victim, user_name=None, kind=JobKind.print, state=JobState.completed),
        ("admin_audit", "actor_id"): AdminAudit(actor_id=victim, actor_name="v", action="x"),
        ("backup_target", "owner_id"): BackupTarget(owner_id=victim, name="b", folder="f"),
        ("quota_request", "decided_by"): QuotaRequest(user_id=other, kind=RequestKind.quota, reason="r", expires_at=NOW,
                                                      decided_by=victim),
        ("quota_grant", "granted_by"): QuotaGrant(user_id=other, metric=Metric.jobs, amount=1, scope=RequestScope.permanent,
                                                  valid_from=NOW, granted_by=victim),
        ("quota_grant", "revoked_by"): QuotaGrant(user_id=other, metric=Metric.jobs, amount=1, scope=RequestScope.permanent,
                                                  valid_from=NOW, revoked_by=victim),
        ("permission_grant", "granted_by"): PermissionGrant(user_id=other, perm="color", valid_from=NOW, granted_by=victim),
        ("permission_grant", "revoked_by"): PermissionGrant(user_id=other, perm="color", valid_from=NOW, revoked_by=victim),
        ("printer_order", "user_id"): PrinterOrder(user_id=victim, printer="p", position=1),
        ("print_key", "user_id"): PrintKey(user_id=victim, label="l", key_prefix="p", key_hash=k),
        ("quota_grant", "user_id"): QuotaGrant(user_id=victim, metric=Metric.pages, amount=1, scope=RequestScope.temporary,
                                               valid_from=NOW, valid_until=NOW + timedelta(days=1)),
        ("quota_request", "user_id"): QuotaRequest(user_id=victim, kind=RequestKind.quota, reason="r", expires_at=NOW),
        ("permission_grant", "user_id"): PermissionGrant(user_id=victim, perm="color", valid_from=NOW),
    }


def leftovers(sm, uid):
    """Every (table, column) that still has a row pointing at `uid`, plus the subject-style tables."""
    out = []
    with sm() as s:
        for table, col in sorted(user_references()):
            if s.execute(text(f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" = :u'), {"u": uid}).scalar():
                out.append((table, col))
        for table in SUBJECT_TABLES:
            if s.execute(text(f'SELECT COUNT(*) FROM "{table}" WHERE subject_type = :t AND subject_id = :u'),
                         {"t": SubjectType.user.name, "u": uid}).scalar():
                out.append((table, "subject_id"))
    return out


def test_deleting_an_account_leaves_nothing_pointing_at_it(portal):
    victim = make_user(portal.sm, "victim", password="x" * 14)
    other = make_user(portal.sm, "other", password="x" * 14)
    rows = rows_for(victim, other)
    assert set(rows) == set(HANDLED), "the live test needs one row for every entry of HANDLED"
    with portal.sm() as s:
        s.add_all([*rows.values(),
                   PolicyRule(subject_type=SubjectType.user, subject_id=victim, limit=5, period=Period.weekly),
                   PermissionRule(subject_type=SubjectType.user, subject_id=victim, color=True)])
        s.commit()

    # the net itself works: before the delete it reports every table, so a table delete_user forgot would show up after
    assert set(leftovers(portal.sm, victim)) == set(HANDLED) | {(t, "subject_id") for t in SUBJECT_TABLES}

    with portal.sm() as s:
        delete_user(s, s.get(User, victim))
        s.commit()

    assert leftovers(portal.sm, victim) == []
    with portal.sm() as s:
        assert s.get(User, victim) is None and s.get(User, other) is not None
        for (table, col), how in HANDLED.items():
            n = s.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar()
            if how == "detached":      # (the "deleted" ones are covered by leftovers() above)
                # the history row is still there, only the link is gone
                assert n >= 1, f"{table} lost its row although {col} should only be detached"
                assert s.execute(text(f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IS NULL')).scalar() >= 1, (table, col)
        # the name survives where the account used to be shown
        assert s.scalars(select(BackupTarget)).one().owner_name == "victim"
