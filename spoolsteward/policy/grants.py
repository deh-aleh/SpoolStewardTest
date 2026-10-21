"""Requests for more quota / another group / a permission, and the grants that approvals produce.

A grant ADDS to the user's limit; it never edits job_log. Temporary grants end with the period of the rule that
limits the user (decided by the admin in the database, never by the user); permanent ones apply to every
following period until revoked.

A permission grant (colour, one-sided, one more printer, any hours) is a different thing: it ends on a date the
administrator chooses (the end of that day in the portal's time zone) or never. `permissions.effective` adds the
ones that are active to what the user's group and own rule give.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from spoolsteward.timeutil import aware
from spoolsteward.auth.users import is_emergency
from spoolsteward.i18n import _
from spoolsteward.audit.adminlog import record
from spoolsteward.config import settings
from spoolsteward.db.models import (
    Group, Metric, PermissionGrant, QuotaGrant, QuotaRequest, RequestKind, RequestScope, RequestStatus, Resource, User,
)
from spoolsteward.policy import permissions
from spoolsteward.policy.engine import active_grants, effective_rules, usage, window_end
from spoolsteward.policy.ordering import key as queue_key

MAX_AMOUNT = 100_000
MAX_TEXT = 500


def _update(model):
    """Conditional UPDATE decided by the database alone. No in-Python re-evaluation against objects already in the
    session (they carry timezone-less datetimes from SQLite): callers re-read what they need."""
    return update(model).execution_options(synchronize_session=False)


class RequestError(ValueError):
    """A request or decision that can not be accepted; the message is safe to show."""


def _clean(text: str, what: str, required: bool = True) -> str:
    """`what`: "reason" or "note" (picks the message; each one is a whole sentence so it can be translated)."""
    text = "".join(ch for ch in text.strip() if ch == "\n" or ord(ch) >= 32)
    if required and not text:
        raise RequestError(_("The reason is required"))
    if len(text) > MAX_TEXT:
        raise RequestError(_("The reason is too long (max %(max)s characters)", max=MAX_TEXT) if what == "reason"
                           else _("The note is too long (max %(max)s characters)", max=MAX_TEXT))
    return text


def _rule_for(session: Session, user: User, metric: Metric):
    return next((r for r in effective_rules(session, user, Resource.print) if r.metric is metric), None)


def quota_options(session: Session, user: User, now: datetime, tz: ZoneInfo) -> list[dict]:
    """What the quota form can offer: only metrics that actually limit the user, with the period the ADMIN set."""
    out = []
    for r in effective_rules(session, user, Resource.print):
        out.append({
            "metric": r.metric.value, "period": r.period.value, "limit": r.limit,
            "extra": active_grants(session, user, r.metric, now),
            "used": usage(session, user, r, now, tz), "window_end": window_end(r.period, now, tz),
        })
    return out


def _open_count(session: Session, user: User, now: datetime) -> int:
    return session.scalar(select(func.count(QuotaRequest.id)).where(
        QuotaRequest.user_id == user.id, QuotaRequest.status == RequestStatus.pending, QuotaRequest.expires_at > now,
    )) or 0


def _new_request(session: Session, user: User, kind: RequestKind, scope, params: dict, reason: str, now: datetime):
    if not user.enabled:
        raise RequestError(_("account disabled"))
    if _open_count(session, user, now) >= settings.max_open_requests:
        raise RequestError(_("you already have %(n)s open requests: wait for an answer or cancel one", n=settings.max_open_requests))
    req = QuotaRequest(user_id=user.id, kind=kind, scope=scope, params_json=params, reason=reason,
                       status=RequestStatus.pending, created_at=now,
                       expires_at=now + timedelta(days=settings.request_ttl_days))
    session.add(req)
    session.commit()
    return req


def create_quota_request(session: Session, user: User, metric: str, amount: int, scope: str, reason: str,
                         now: datetime) -> QuotaRequest:
    try:
        m, sc = Metric(metric), RequestScope(scope)
    except ValueError:
        raise RequestError(_("invalid request")) from None
    if not isinstance(amount, int) or not 1 <= amount <= MAX_AMOUNT:
        raise RequestError(_("the amount must be between 1 and %(max)s", max=MAX_AMOUNT))
    if _rule_for(session, user, m) is None:
        raise RequestError(_("no %(metric)s limit applies to you, there is nothing to ask for", metric=_(m.value)))
    # NB: the period is deliberately not a parameter. It comes from the rule the admin set.
    return _new_request(session, user, RequestKind.quota, sc, {"metric": m.value, "amount": amount},
                        _clean(reason, "reason", settings.request_reason_required), now)


def create_group_request(session: Session, user: User, target_group_id: int, reason: str, now: datetime) -> QuotaRequest:
    if is_emergency(user):
        raise RequestError(_("this account has no group"))
    group = session.get(Group, target_group_id)
    if group is None or not group.requestable:
        raise RequestError(_("that group can not be requested"))
    if user.group_id == group.id:
        raise RequestError(_("you are already in that group"))
    return _new_request(session, user, RequestKind.group_change, None, {"target_group_id": group.id},
                        _clean(reason, "reason", settings.request_reason_required), now)


def _permission_params(perm: str, printer: str | None) -> tuple[str, str | None]:
    """Check what a permission is about and return it in its stored form (`printer` lower-case, and only for "printer")."""
    if perm not in permissions.GRANTABLE:
        raise RequestError(_("invalid request"))
    if perm != "printer":
        return perm, None
    name = (printer or "").strip()
    if not permissions.NAME_RE.match(name):
        raise RequestError(_("invalid printer name"))
    return perm, queue_key(name)


def create_permission_request(session: Session, user: User, perm: str, printer: str | None, reason: str,
                              now: datetime) -> QuotaRequest:
    """The user asks for colour, one-sided printing, one more printer or printing at any hour. Refused when they have it
    already, or have asked for the same and wait for the answer."""
    perm, printer = _permission_params(perm, printer)
    reason = _clean(reason, "reason", settings.request_reason_required)
    if permissions.has_permission(permissions.effective(session, user, now), perm, printer):
        raise RequestError(_("you already have this permission"))
    waiting = session.scalars(select(QuotaRequest).where(
        QuotaRequest.user_id == user.id, QuotaRequest.kind == RequestKind.permission,
        QuotaRequest.status == RequestStatus.pending, QuotaRequest.expires_at > now))
    if any((r.params_json or {}).get("perm") == perm and (r.params_json or {}).get("printer") == printer for r in waiting):
        raise RequestError(_("you already asked for this: wait for the answer"))
    return _new_request(session, user, RequestKind.permission, None, {"perm": perm, "printer": printer}, reason, now)


def cancel_request(session: Session, req: QuotaRequest, user: User, now: datetime) -> None:
    if req.user_id != user.id:
        raise RequestError(_("not your request"))
    done = session.execute(_update(QuotaRequest).where(
        QuotaRequest.id == req.id, QuotaRequest.status == RequestStatus.pending,
    ).values(status=RequestStatus.canceled, decided_at=now))
    session.commit()
    if done.rowcount != 1:
        raise RequestError(_("the request was already decided"))


def expire_requests(session: Session, now: datetime) -> int:
    done = session.execute(_update(QuotaRequest).where(
        QuotaRequest.status == RequestStatus.pending, QuotaRequest.expires_at <= now,
    ).values(status=RequestStatus.expired, decided_at=now))
    session.commit()
    return done.rowcount


def _make_grant(session: Session, user: User, metric: Metric, amount: int, scope: RequestScope, admin: User,
                now: datetime, tz: ZoneInfo, request_id: int | None, note: str | None) -> QuotaGrant:
    rule = _rule_for(session, user, metric)
    if rule is None:
        raise RequestError(_("no %(metric)s limit applies to %(user)s any more: nothing to extend", metric=_(metric.value), user=user.username))
    until = window_end(rule.period, now, tz) if scope is RequestScope.temporary else None
    grant = QuotaGrant(user_id=user.id, metric=metric, amount=amount, scope=scope, valid_from=now, valid_until=until,
                       request_id=request_id, granted_by=admin.id, note=note, created_at=now)
    session.add(grant)
    return grant


def end_of_day(day: date, tz: ZoneInfo) -> datetime:
    """The moment the given date is over in `tz` (UTC), so "until 2026-11-30" includes the whole of the 30th."""
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)


def last_day(valid_until: datetime | None, tz: ZoneInfo) -> str | None:
    """The date to show for the end of a permission grant, 'YYYY-MM-DD': it is valid through the whole of that day.
    None = no end."""
    if valid_until is None:
        return None
    return (aware(valid_until).astimezone(tz) - timedelta(seconds=1)).date().isoformat()


def _make_permission_grant(session: Session, user: User, perm: str, printer: str | None, until: date | None, admin: User,
                           now: datetime, tz: ZoneInfo, request_id: int | None, note: str | None) -> PermissionGrant:
    perm, printer = _permission_params(perm, printer)
    end = None
    if until is not None:
        end = end_of_day(until, tz)
        if end <= now:
            raise RequestError(_("the end date must be in the future"))
    grant = PermissionGrant(user_id=user.id, perm=perm, printer=printer, valid_from=now, valid_until=end,
                            request_id=request_id, granted_by=admin.id, note=note, created_at=now)
    session.add(grant)
    return grant


def approve_request(session: Session, req: QuotaRequest, admin: User, now: datetime, tz: ZoneInfo,
                    amount: int | None = None, scope: str | None = None, note: str = "",
                    until: date | None = None) -> QuotaGrant | PermissionGrant | None:
    """Idempotent: the status flip is a single conditional UPDATE, so a double click or two admins at once
    grant once. The admin may change amount and scope of a quota request before approving. `until`: for a permission
    request, the last day it is valid (None = permanent)."""
    note = _clean(note, "note", required=False)
    user = session.get(User, req.user_id)
    if user is None or not user.enabled:
        raise RequestError(_("the user is disabled"))
    if aware(req.expires_at) <= now and req.status is RequestStatus.pending:
        expire_requests(session, now)
        raise RequestError(_("the request has expired"))
    claimed = session.execute(_update(QuotaRequest).where(
        QuotaRequest.id == req.id, QuotaRequest.status == RequestStatus.pending,
    ).values(status=RequestStatus.approved, decided_by=admin.id, decided_at=now, decision_note=note or None))
    if claimed.rowcount != 1:
        session.rollback()
        raise RequestError(_("the request was already decided"))
    grant = None
    try:
        if req.kind is RequestKind.quota:
            metric = Metric(req.params_json["metric"])
            amt = req.params_json["amount"] if amount is None else amount
            if not isinstance(amt, int) or not 1 <= amt <= MAX_AMOUNT:
                raise RequestError(_("the amount must be between 1 and %(max)s", max=MAX_AMOUNT))
            sc = req.scope if scope is None else RequestScope(scope)
            grant = _make_grant(session, user, metric, amt, sc, admin, now, tz, req.id, note or None)
            record(session, admin, "request.approve", "quota_request", req.id, user=user.username, kind="quota",
                   metric=metric.value, amount=amt, scope=sc.value, requested_amount=req.params_json["amount"],
                   requested_scope=req.scope.value)
        elif req.kind is RequestKind.permission:
            grant = _make_permission_grant(session, user, req.params_json["perm"], req.params_json.get("printer"), until,
                                           admin, now, tz, req.id, note or None)
            record(session, admin, "request.approve", "quota_request", req.id, user=user.username, kind="permission",
                   perm=grant.perm, printer=grant.printer, until=until.isoformat() if until else None)
        else:
            group = session.get(Group, req.params_json["target_group_id"])
            if group is None:
                raise RequestError(_("the requested group no longer exists"))
            old = user.group_id
            user.group_id = group.id
            record(session, admin, "request.approve", "quota_request", req.id, user=user.username,
                   kind="group_change", from_group_id=old, to_group_id=group.id)
        session.commit()
    except (RequestError, ValueError, KeyError):
        session.rollback()  # also undoes the status flip: a refused approval leaves the request pending
        raise
    return grant


def reject_request(session: Session, req: QuotaRequest, admin: User, now: datetime, note: str = "") -> None:
    note = _clean(note, "note", required=False)
    done = session.execute(_update(QuotaRequest).where(
        QuotaRequest.id == req.id, QuotaRequest.status == RequestStatus.pending,
    ).values(status=RequestStatus.rejected, decided_by=admin.id, decided_at=now, decision_note=note or None))
    if done.rowcount != 1:
        session.rollback()
        raise RequestError(_("the request was already decided"))
    record(session, admin, "request.reject", "quota_request", req.id, kind=req.kind.value, note=note)
    session.commit()


def create_grant(session: Session, admin: User, user: User, metric: str, amount: int, scope: str, note: str,
                 now: datetime, tz: ZoneInfo) -> QuotaGrant:
    """A grant the admin hands out without a request."""
    try:
        m, sc = Metric(metric), RequestScope(scope)
    except ValueError:
        raise RequestError(_("invalid grant")) from None
    if not isinstance(amount, int) or not 1 <= amount <= MAX_AMOUNT:
        raise RequestError(_("the amount must be between 1 and %(max)s", max=MAX_AMOUNT))
    grant = _make_grant(session, user, m, amount, sc, admin, now, tz, None, _clean(note, "note", required=False) or None)
    session.flush()
    record(session, admin, "grant.create", "quota_grant", grant.id, user=user.username, metric=m.value,
           amount=amount, scope=sc.value)
    session.commit()
    return grant


def create_permission_grant(session: Session, admin: User, user: User, perm: str, printer: str | None, until: date | None,
                            note: str, now: datetime, tz: ZoneInfo) -> PermissionGrant:
    """A permission the admin hands out without a request. `until`: the last day it is valid (None = permanent)."""
    perm, printer = _permission_params(perm, printer)
    if permissions.has_permission(permissions.effective(session, user, now), perm, printer):
        raise RequestError(_("%(user)s already has this permission", user=user.username))
    grant = _make_permission_grant(session, user, perm, printer, until, admin, now, tz, None,
                                   _clean(note, "note", required=False) or None)
    session.flush()
    record(session, admin, "grant.create", "permission_grant", grant.id, user=user.username, perm=grant.perm,
           printer=grant.printer, until=until.isoformat() if until else None)
    session.commit()
    return grant


def revoke_permission_grant(session: Session, grant: PermissionGrant, admin: User, now: datetime) -> None:
    done = session.execute(_update(PermissionGrant).where(PermissionGrant.id == grant.id, PermissionGrant.revoked_at.is_(None))
                           .values(revoked_at=now, revoked_by=admin.id))
    if done.rowcount != 1:
        session.rollback()
        raise RequestError(_("the grant was already revoked"))
    record(session, admin, "grant.revoke", "permission_grant", grant.id, user_id=grant.user_id, perm=grant.perm,
           printer=grant.printer)
    session.commit()


def revoke_grant(session: Session, grant: QuotaGrant, admin: User, now: datetime) -> None:
    done = session.execute(_update(QuotaGrant).where(QuotaGrant.id == grant.id, QuotaGrant.revoked_at.is_(None))
                           .values(revoked_at=now, revoked_by=admin.id))
    if done.rowcount != 1:
        session.rollback()
        raise RequestError(_("the grant was already revoked"))
    record(session, admin, "grant.revoke", "quota_grant", grant.id, user_id=grant.user_id, metric=grant.metric.value)
    session.commit()


def purge_ended(session: Session, admin: User, now: datetime) -> int:
    """Delete the grants that count no more (quota and permission): revoked, or whose end has passed. Active ones are
    never touched. Recorded in the administrators' log (with how many of each kind), which keeps the history of every
    grant anyway. Returns the total."""
    quota = session.execute(delete(QuotaGrant).where(
        QuotaGrant.revoked_at.is_not(None) | (QuotaGrant.valid_until.is_not(None) & (QuotaGrant.valid_until <= now)),
    ).execution_options(synchronize_session=False)).rowcount
    perm = session.execute(delete(PermissionGrant).where(
        PermissionGrant.revoked_at.is_not(None) | (PermissionGrant.valid_until.is_not(None) & (PermissionGrant.valid_until <= now)),
    ).execution_options(synchronize_session=False)).rowcount
    if quota or perm:
        record(session, admin, "grant.purge", "quota_grant", None, count=quota + perm, quota=quota, permission=perm)
    session.commit()
    return quota + perm
