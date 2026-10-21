"""Administration: users, groups, limits, keys and the log of administrative actions."""

import logging
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.timeutil import local_tz
from app.admin.common import (
    back, current_admin, flash, get_db, localtime, parse_limit, render, verify_csrf,
)
from app import notices
from app.admin.printers import NAME_RE
from app.audit.adminlog import record
from app.auth import keys as keylib
from app.auth.groups import ADMIN_GROUP, set_admin, system_group_names
from app.auth.users import default_group_id, delete_user, is_emergency
from app.auth.passwords import hash_password, validate_new_password
from app.config import USERNAME_RE, settings
from app.cups_client import list_queues
from app.i18n import _, ngettext
from app.i18n import html_pgettext as pgettext
from app.db.models import AdminAudit, Group, JobLog, Metric, Period, PrintKey, Resource, SubjectType, User
from app.policy import grants, permissions
from app.policy.engine import active_grant_list, effective_limit, effective_rules, usage
from app.policy.ordering import key as queue_key
from app.policy.rules import get_limits, set_limit

log = logging.getLogger("app.admin")
ACTIONS_PAGE = 30  # rows per page in the administrative log
router = APIRouter(prefix="/admin")


def _usage_lines(db: Session, user: User) -> list[str]:
    now, tz = datetime.now(UTC), local_tz()
    return [_("%(used)s/%(limit)s %(metric)s (%(period)s)", used=usage(db, user, r, now, tz), limit=effective_limit(db, user, r, now),
              metric=_(r.metric.value), period=_(r.period.value))
            for r in effective_rules(db, user, Resource.print)]


def _other_admins(db: Session, user: User) -> int:
    return db.scalar(select(func.count(User.id)).where(User.is_admin, User.enabled, User.id != user.id)) or 0


def _protected_admin(db: Session, user: User, acting: User) -> str | None:
    """Why this account may not lose admin rights / be disabled, or None."""
    if user.id == acting.id:
        return _("You cannot disable or demote yourself")
    if is_emergency(user):
        return _("%(user)s is the emergency administrator: it can not be disabled or demoted", user=user.username)
    if user.is_admin and user.enabled and _other_admins(db, user) == 0:
        return _("That is the last administrator")
    return None


# ------------------------------------------------------------------ advanced permissions (form <-> rule)

def _permission_flag(raw: str, inherit: bool) -> bool | None:
    raw = raw.strip()
    if raw not in ("", "0", "1"):
        raise ValueError(_("Invalid permission value"))
    return None if raw == "" and inherit else raw == "1"


def _permission_printers(printers: list[str]) -> list[str]:
    names = []
    for raw in printers:
        name = raw.strip()
        if not NAME_RE.match(name):
            raise ValueError(_("Invalid printer name %(name)s", name=repr(name[:40])))
        names.append(queue_key(name))
    names = sorted(set(names))
    if not names:
        raise ValueError(_("Choose at least one printer, or choose “all printers”"))
    if len(names) > permissions.MAX_PRINTERS:
        raise ValueError(_("Too many printers in the list (at most %(max)s)", max=permissions.MAX_PRINTERS))
    return names


def _permission_window(days: list[str], start: str, end: str) -> tuple[str, int, int]:
    if not days or any(d not in tuple("0123456") for d in days):
        raise ValueError(_("Choose at least one day of the week for the hours"))
    try:
        lo, hi = permissions.parse_time(start), permissions.parse_time(end)
    except ValueError:
        raise ValueError(_("Enter the hours as HH:MM, for example 08:00 and 17:30")) from None
    if lo == hi:
        raise ValueError(_("The start and the end of the hours must be different"))
    return "".join(sorted(set(days))), lo, hi


def _parse_permissions(inherit: bool, color: str, one_sided: str, printers_mode: str, printers: list[str], hours_mode: str,
                       days: list[str], start: str, end: str) -> dict:
    """The permission fields of a user form (`inherit`: an empty choice means "as the group") or of a group form (an empty
    choice is an unchecked box, and the defaults are stored as nothing). Raises ValueError with the message to show."""
    color, one_sided = _permission_flag(color, inherit), _permission_flag(one_sided, inherit)
    if not inherit:  # a group that allows it has nothing stored: no row means everything allowed
        color, one_sided = (None if color else False), (None if one_sided else False)
    out = {"color": color, "one_sided": one_sided, "printers_mode": None, "printers": None,
           "hours_mode": None, "hours_days": None, "hours_start": None, "hours_end": None}
    if printers_mode not in ("", "all", "list") or (printers_mode == "" and not inherit):
        raise ValueError(_("Invalid permission value"))
    if printers_mode == "all" and not inherit:
        printers_mode = ""  # a group that allows every printer has nothing stored
    out["printers_mode"] = printers_mode or None
    if printers_mode == "list":
        out["printers"] = _permission_printers(printers)
    if hours_mode not in ("", "any", "window") or (hours_mode == "" and not inherit):
        raise ValueError(_("Invalid permission value"))
    if hours_mode == "any" and not inherit:
        hours_mode = ""
    out["hours_mode"] = hours_mode or None
    if hours_mode == "window":
        out["hours_days"], out["hours_start"], out["hours_end"] = _permission_window(days, start, end)
    return out


def _permission_changes(old: dict, new: dict) -> dict:
    return {k: [old[k], new[k]] for k in new if old[k] != new[k]}


def _tri(value: bool | None, inherit: bool) -> str:
    return ("" if inherit else "1") if value is None else ("1" if value else "0")


async def _permission_form(request: Request, db: Session, rule, *, inherit: bool, group_id: int | None = None,
                           disabled: bool = False) -> dict:
    """What the "Advanced permissions" block of a user (`inherit`) or group page shows."""
    f = permissions.rule_fields(rule)
    current = [q["name"] for q in await list_queues(request.app)]
    saved = set(f["printers"] or [])
    shown = {queue_key(n) for n in current}
    group = permissions.group_perms(db, group_id) if inherit else None
    out = {
        "inherit": inherit, "disabled": disabled,
        "color": _tri(f["color"], inherit), "one_sided": _tri(f["one_sided"], inherit),
        "printers_mode": f["printers_mode"] or ("" if inherit else "all"),
        "queues": [{"name": n, "checked": queue_key(n) in saved, "present": True} for n in current]
                  + [{"name": n, "checked": True, "present": False} for n in sorted(saved - shown)],
        "hours_mode": f["hours_mode"] or ("" if inherit else "any"),
        "days": {int(c) for c in f["hours_days"] or ""} or {0, 1, 2, 3, 4},
        "start": permissions.fmt_minutes(f["hours_start"]) if f["hours_start"] is not None else "08:00",
        "end": permissions.fmt_minutes(f["hours_end"]) if f["hours_end"] is not None else "17:00",
        "day_names": [(i, _(n)) for i, n in enumerate(permissions.DAYS)],
        "open": any(v is not None for v in f.values()),
    }
    if group is not None:
        out["group"] = {
            "color": _("allowed") if group.color else _("not allowed"),
            "one_sided": _("allowed") if group.one_sided else _("not allowed"),
            "printers": pgettext("printers", "all") if group.printers is None else (", ".join(sorted(group.printers)) or _("none")),
            "hours": _("any time") if group.window is None else permissions.describe_window(group.window),
        }
    return out


# ------------------------------------------------------------------ users

def _limit_view(db: Session, u: User, now) -> dict:
    """What the list shows for one user: effective limits (own or inherited from the group), active grants, usage."""
    rules = {r.metric: r for r in effective_rules(db, u, Resource.print)}
    own = get_limits(db, SubjectType.user, u.id)
    out = {"usage": _usage_lines(db, u)}
    for m in (Metric.jobs, Metric.pages):
        r = rules.get(m)
        inherited = bool(r) and m not in own
        grants = active_grant_list(db, u, m, now) if r else []
        # tooltip: where the limit comes from, then every active grant with its expiry
        tip = ([_("limit inherited from the group")] if inherited else []) + [
            f"+{g.amount} · " + (_("temporary, expires on %(date)s", date=localtime(g.valid_until, '%Y-%m-%d %H:%M')) if g.valid_until else _("permanent"))
            for g in grants]
        out[m.value] = {
            "limit": r.limit if r else None, "inherited": inherited,
            "extra": sum(g.amount for g in grants), "period": r.period.value if r else None, "tip": "\n".join(tip),
        }
    out["period"] = next((r.period.value for r in rules.values()), "weekly")
    return out


@router.get("/users", response_class=HTMLResponse)
def users(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    now = datetime.now(UTC)
    group_names = {g.id: g.name for g in db.scalars(select(Group))}
    key_counts = dict(db.execute(select(PrintKey.user_id, func.count(PrintKey.id)).where(
        PrintKey.revoked_at.is_(None), PrintKey.disabled_at.is_(None), PrintKey.used_at.is_(None),
        (PrintKey.expires_at.is_(None)) | (PrintKey.expires_at > now))
        .group_by(PrintKey.user_id)).all())
    rows = [{"u": u, "group": group_names.get(u.group_id, "—"), "key_count": key_counts.get(u.id, 0),
             "view": _limit_view(db, u, now), "protected": is_emergency(u)}
            for u in db.scalars(select(User).order_by(User.username))]
    groups = db.scalars(select(Group).where(Group.name != ADMIN_GROUP).order_by(Group.name)).all()  # administrators are made with the checkbox
    return render(request, "users.html", admin, rows=rows, groups=groups, default_group=settings.default_group)


def _is_admin_group(db: Session, group_id: int) -> bool:
    g = db.get(Group, group_id) if group_id else None
    return g is not None and g.name == ADMIN_GROUP


def _providers(request: Request) -> list[str]:
    auth = getattr(request.app.state, "auth", None)
    return ["local"] + ([auth.name] if auth is not None else [])


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def user_edit(request: Request, user_id: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404)
    v = _limit_view(db, u, datetime.now(UTC))
    own = get_limits(db, SubjectType.user, u.id)
    perm = await _permission_form(request, db, permissions.get_rule(db, SubjectType.user, u.id), inherit=True,
                                  group_id=u.group_id, disabled=is_emergency(u))
    now = datetime.now(UTC)
    perm_grants = [{"what": permissions.describe_grant(g.perm, g.printer), "until": grants.last_day(g.valid_until, local_tz())}
                   for g in permissions.active_grants(db, u, now)]
    return render(request, "user_edit.html", admin, u=u, view=v, perm=perm, perm_grants=perm_grants, groups=db.scalars(select(Group).order_by(Group.name)).all(),
                  jobs=own[Metric.jobs].limit if Metric.jobs in own else "",
                  pages=own[Metric.pages].limit if Metric.pages in own else "",
                  periods=[p.value for p in Period], providers=_providers(request), admin_group=ADMIN_GROUP,
                  protected=is_emergency(u), is_self=u.id == admin.id)


def _temp_password() -> str:
    return secrets.token_urlsafe(12)


@router.post("/users/local", dependencies=[Depends(verify_csrf)])
def user_create_local(request: Request, username: str = Form(...), group_id: int = Form(0), password: str = Form(""),
                      is_admin: str = Form(""), admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    name = username.strip().lower()
    generated = not password
    pw = password or _temp_password()
    error = (
        _("Unknown group") if group_id and db.get(Group, group_id) is None else
        _('Group "%(group)s" is only for administrators: tick the administrator box instead', group=ADMIN_GROUP) if _is_admin_group(db, group_id) else
        _("Invalid username (letters, digits, dot, dash, underscore; max 63)") if not USERNAME_RE.match(name) else
        _("User %(name)s already exists", name=name) if db.scalars(select(User).where(User.username == name)).first() else
        _("The name %(name)s is reserved for the emergency administrator", name=name) if name == settings.bootstrap_admin_username else
        None if generated else validate_new_password(pw, name)
    )
    if error:
        flash(request, error, "err")
        return back("/admin/users")
    user = User(provider="local", username=name, group_id=group_id or default_group_id(db, settings.default_group),
                password_hash=hash_password(pw), must_change_password=True,
                password_changed_at=datetime.now(UTC))
    db.add(user)
    db.flush()
    if is_admin:
        set_admin(db, user, True)
    record(db, admin, "user.create", "user", user.id, username=name, provider="local", is_admin=bool(is_admin))
    db.commit()
    log.info("admin %s created local user %s", admin.username, name)
    return render(request, "secret_shown.html", admin, title=_("User %(name)s created", name=name), what=_("Temporary password"),
                  secret=pw, note=_("Give it to the user over a safe channel. They must choose their own at first login."),
                  next_url="/admin/users")


@router.post("/users", dependencies=[Depends(verify_csrf)])
def user_create(request: Request, username: str = Form(...), group_id: int = Form(0),
                admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    """Pre-register someone who will log in through the delegated provider (RADIUS)."""
    provider = getattr(request.app.state, "auth", None)
    name = username.strip().lower()
    if provider is None:
        flash(request, _("RADIUS is not configured: create a local user instead"), "err")
    elif group_id and db.get(Group, group_id) is None:
        flash(request, _("Unknown group"), "err")
    elif _is_admin_group(db, group_id):
        flash(request, _('Group "%(group)s" is only for administrators: make them administrator from their page', group=ADMIN_GROUP), "err")
    elif not name or len(name) > 128:
        flash(request, _("Invalid username"), "err")
    elif db.scalars(select(User).where(User.username == name)).first():
        flash(request, _("User %(name)s already exists", name=name), "err")
    else:
        user = User(provider=provider.name, username=name, group_id=group_id or default_group_id(db, settings.default_group))
        db.add(user)
        db.flush()
        record(db, admin, "user.create", "user", user.id, username=name, provider=provider.name)
        db.commit()
        log.info("admin %s created user %s", admin.username, name)
        flash(request, _("User %(name)s created", name=name))
    return back("/admin/users")


class _Refuse(Exception):
    """A form problem the administrator is told about with a message on the edit page."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _check_update(db: Session, request: Request, admin: User, u: User, group_id: int, enabled: str, is_admin: str,
                  provider: str) -> None:
    """What may not be saved, before anything is changed (raises _Refuse with the message to show)."""
    if group_id and db.get(Group, group_id) is None:
        raise _Refuse(_("Unknown group"))
    if provider not in _providers(request) and provider != u.provider:
        raise _Refuse(_("Unknown or unavailable login method"))
    if not enabled or not is_admin:  # disabling or demoting: never yourself, the emergency admin, or the last admin
        reason = _protected_admin(db, u, admin)
        if reason:
            raise _Refuse(reason)
    if provider == "local" and provider != u.provider and u.username == settings.bootstrap_admin_username:
        raise _Refuse(_("The name %(name)s is reserved for the emergency administrator", name=u.username))
    if provider != u.provider and is_emergency(u):
        raise _Refuse(_("%(user)s is the emergency administrator: it must stay a local account", user=u.username))


def _admin_and_group_changes(db: Session, u: User, group_id: int, enabled: str, is_admin: str) -> dict:
    """The administrator right and the group of an account, set as the form says; returns what changed."""
    admin_group = db.scalars(select(Group).where(Group.name == ADMIN_GROUP)).first()
    changes = {}
    if u.enabled != bool(enabled):
        changes["enabled"] = bool(enabled)
    if u.is_admin != bool(is_admin):
        changes["is_admin"] = bool(is_admin)
        old_group = u.group_id
        set_admin(db, u, bool(is_admin))  # administrators live in "administrator"; demoted ones go back to "default"
        if not is_admin and group_id and not _is_admin_group(db, group_id):
            u.group_id = group_id  # demoted and a group chosen in the same save: the chosen one wins over "default"
        if u.group_id != old_group:
            changes["group_id"] = [old_group, u.group_id]
    elif (u.group_id or 0) != group_id:
        if admin_group is not None and group_id == admin_group.id and not u.is_admin:
            raise _Refuse(_('Group "%(group)s" is only for administrators', group=ADMIN_GROUP))
        if u.is_admin and (admin_group is None or group_id != admin_group.id):
            raise _Refuse(_('Administrators stay in "%(group)s": remove the admin right first', group=ADMIN_GROUP))
        changes["group_id"] = [u.group_id, group_id or None]
        u.group_id = group_id or None
    return changes


def _switch_provider(u: User, provider: str, changes: dict) -> str | None:
    """Moves the account to another login method; returns the temporary password when it becomes a local account."""
    if provider == u.provider:
        return None
    temp = None
    changes["provider"] = [u.provider, provider]
    if provider == "local":  # becomes a local account: a temporary password, shown once
        temp = _temp_password()
        u.password_hash, u.must_change_password = hash_password(temp), True
    else:  # handed over to RADIUS: the stored password must not remain a second way in
        u.password_hash, u.must_change_password = None, False
    u.provider = provider
    u.password_changed_at = datetime.now(UTC)  # ends the user's current portal sessions
    return temp


def _save_permissions(db: Session, u: User, perm: dict | None) -> dict:
    """Stores the permissions the form asked for (none: left alone); returns what changed."""
    if perm is None:
        return {}
    perm_changes = _permission_changes(permissions.rule_fields(permissions.get_rule(db, SubjectType.user, u.id)), perm)
    if perm_changes:
        permissions.set_rule(db, SubjectType.user, u.id, **perm)
    return perm_changes


@router.post("/users/{user_id}", dependencies=[Depends(verify_csrf)])
def user_update(request: Request, user_id: int, group_id: int = Form(0), enabled: str = Form(""),
                is_admin: str = Form(""), jobs_limit: str = Form(""), pages_limit: str = Form(""),
                period: str = Form("weekly"), provider: str = Form(""),
                perm_color: str = Form(""), perm_one_sided: str = Form(""), perm_printers: str = Form(""),
                perm_printer: list[str] = Form([]), perm_hours: str = Form(""), perm_day: list[str] = Form([]),
                perm_start: str = Form(""), perm_end: str = Form(""), perm_form: str = Form(""),
                admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404)
    edit = f"/admin/users/{u.id}/edit"
    emergency = is_emergency(u)  # no group, no limits, whatever is posted
    if emergency:
        group_id, jobs_limit, pages_limit = 0, "", ""
    try:
        jl, pl, per = parse_limit(jobs_limit), parse_limit(pages_limit), Period(period)
        # the emergency administrator has every permission whatever is posted: nothing is read, nothing is stored
        # (`perm_form` says the block was on the page: a post without it leaves the permissions alone)
        perm = None if is_emergency(u) or not perm_form else _parse_permissions(
            True, perm_color, perm_one_sided, perm_printers, perm_printer, perm_hours, perm_day, perm_start, perm_end)
    except ValueError as exc:
        flash(request, str(exc), "err")
        return back(edit)
    provider = provider or u.provider
    try:
        _check_update(db, request, admin, u, group_id, enabled, is_admin, provider)
        changes = _admin_and_group_changes(db, u, group_id, enabled, is_admin)
    except _Refuse as stop:
        flash(request, stop.message, "err")
        return back(edit)
    u.enabled = bool(enabled)
    temp = _switch_provider(u, provider, changes)
    if not emergency:
        set_limit(db, SubjectType.user, u.id, Metric.jobs, jl, per)
        set_limit(db, SubjectType.user, u.id, Metric.pages, pl, per)
    perm_changes = _save_permissions(db, u, perm)
    record(db, admin, "user.update", "user", u.id, username=u.username,
           **({} if emergency else {"limits": {"jobs": jl, "pages": pl, "period": per.value}}),
           **({"permissions": perm_changes} if perm_changes else {}), **changes)
    db.commit()
    log.info("admin %s updated user %s", admin.username, u.username)
    _tell_account_change(request, db, admin, u, changes)
    if perm_changes and u.id != admin.id:  # their own rule changed (not the group's): they find it in their bell
        notices.notify_user(request.app.state.sessionmaker, u.id, "permissions_changed", {})
    if temp:
        return render(request, "secret_shown.html", admin, title=_("%(user)s is now a local account", user=u.username),
                      what=_("Temporary password"), secret=temp,
                      note=_("The user must choose their own at the next login. Their print keys keep working."),
                      next_url="/admin/users")
    flash(request, _("Saved %(name)s", name=u.username))
    return back("/admin/users")


def _group_name(db: Session, group_id: int | None) -> str:
    g = db.get(Group, group_id) if group_id else None
    return g.name if g else ""


def _tell_account_change(request: Request, db: Session, admin: User, u: User, changes: dict) -> None:
    """The person finds in their bell that an administrator changed their group or their administrator right."""
    if u.id == admin.id:
        return
    sm = request.app.state.sessionmaker
    if "is_admin" in changes:
        if u.is_admin:
            notices.notify_user(sm, u.id, "admin_on", {})
        else:
            notices.notify_user(sm, u.id, "admin_off", {"group": _group_name(db, u.group_id)})
    elif "group_id" in changes:
        old, new = changes["group_id"]
        notices.notify_user(sm, u.id, "group_changed", {"old": _group_name(db, old), "new": _group_name(db, new)})


@router.get("/users/{user_id}/delete", response_class=HTMLResponse)
def user_delete_confirm(request: Request, user_id: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404)
    reason = _delete_blocked(db, u, admin)
    if reason:
        flash(request, reason, "err")
        return back(f"/admin/users/{u.id}/edit")
    jobs = db.scalar(select(func.count(JobLog.id)).where(JobLog.user_id == u.id)) or 0
    return render(request, "user_delete.html", admin, u=u, jobs=jobs)


def _delete_blocked(db: Session, u: User, acting: User) -> str | None:
    if u.id == acting.id:
        return _("You cannot delete yourself")
    if is_emergency(u):
        return _("%(user)s is the emergency administrator: it can not be deleted", user=u.username)
    if u.is_admin and u.enabled and _other_admins(db, u) == 0:
        return _("That is the last administrator")
    return None


@router.post("/users/{user_id}/delete", dependencies=[Depends(verify_csrf)])
def user_delete(request: Request, user_id: int, confirm_name: str = Form(""), admin: User = Depends(current_admin),
                db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404)
    reason = _delete_blocked(db, u, admin)
    if reason is None and confirm_name.strip().lower() != u.username:
        reason = _("The name you typed does not match: nothing was deleted")
    if reason:
        flash(request, reason, "err")
        return back(f"/admin/users/{u.id}/delete")
    summary = delete_user(db, u)
    record(db, admin, "user.delete", "user", user_id, **summary)
    db.commit()
    log.info("admin %s deleted user %s", admin.username, summary["username"])
    flash(request, _("Account %(name)s deleted (%(n)s print record(s) kept in the audit)", name=summary["username"], n=summary["jobs_kept"]))
    return back("/admin/users")


@router.post("/users/{user_id}/password", dependencies=[Depends(verify_csrf)])
def user_reset_password(request: Request, user_id: int, admin: User = Depends(current_admin),
                        db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404)
    if u.provider != "local":
        flash(request, _("That account signs in through RADIUS: its password is managed there"), "err")
        return back("/admin/users")
    pw = _temp_password()
    u.password_hash, u.must_change_password = hash_password(pw), True
    u.password_changed_at = datetime.now(UTC)  # ends the user's current portal sessions
    record(db, admin, "user.reset_password", "user", u.id, username=u.username)
    db.commit()
    return render(request, "secret_shown.html", admin, title=_("New password for %(user)s", user=u.username),
                  what=_("Temporary password"), secret=pw,
                  note=_("The user must choose a new one at the next login. Their print keys keep working."),
                  next_url="/admin/users")


@router.post("/users/{user_id}/revoke-keys", dependencies=[Depends(verify_csrf)])
def user_revoke_keys(request: Request, user_id: int, admin: User = Depends(current_admin),
                     db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404)
    n = keylib.revoke_all(db, u)
    record(db, admin, "user.revoke_keys", "user", u.id, username=u.username, revoked=n)
    db.commit()
    if n and u.id != admin.id:
        notices.notify_user(request.app.state.sessionmaker, u.id, "key_revoked_many", {"count": n})
    flash(request, _("%(n)s key(s) of %(user)s revoked", n=n, user=u.username))
    return back(f"/admin/users/{u.id}/keys")


@router.get("/users/{user_id}/keys", response_class=HTMLResponse)
def user_keys(request: Request, user_id: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(404)
    now = datetime.now(UTC)
    rows = db.scalars(select(PrintKey).where(PrintKey.user_id == u.id).order_by(PrintKey.id.desc())).all()
    return render(request, "keys.html", admin, owner=u, keys=keylib.listing(rows, now), choices=keylib.duration_choices(u), default_choice=str(keylib.default_duration(u)),
                  base=f"/admin/users/{u.id}/keys", own=False)


@router.post("/users/{user_id}/keys/{key_id}/revoke", dependencies=[Depends(verify_csrf)])
def user_key_revoke(request: Request, user_id: int, key_id: int, admin: User = Depends(current_admin),
                    db: Session = Depends(get_db)):
    k = db.get(PrintKey, key_id)
    if k is None or k.user_id != user_id:
        raise HTTPException(404)
    if keylib.revoke_key(db, k):
        record(db, admin, "key.revoke", "print_key", k.id, username=db.get(User, user_id).username, label=k.label)
        db.commit()
        _tell_user(request, admin, k, "key_revoked")
        flash(request, _("Key %(label)s revoked", label=repr(k.label)))
    return back(f"/admin/users/{user_id}/keys")


def _tell_user(request: Request, admin: User, k: PrintKey, kind: str) -> None:
    """The owner of the key finds it in their own bell (not when the administrator is the owner)."""
    if k.user_id != admin.id:
        notices.notify_user(request.app.state.sessionmaker, k.user_id, kind,
                            {"label": k.label, "prefix": k.key_prefix,
                             "until": k.expires_at.isoformat() if k.expires_at else None})


def _owned_key(db: Session, user_id: int, key_id: int) -> PrintKey:
    k = db.get(PrintKey, key_id)
    if k is None or k.user_id != user_id:
        raise HTTPException(404)
    return k


@router.post("/users/{user_id}/keys/{key_id}/disable", dependencies=[Depends(verify_csrf)])
def user_key_disable(request: Request, user_id: int, key_id: int, admin: User = Depends(current_admin),
                     db: Session = Depends(get_db)):
    k = _owned_key(db, user_id, key_id)
    if keylib.disable_key(db, k):
        record(db, admin, "key.disable", "print_key", k.id, username=db.get(User, user_id).username, label=k.label)
        db.commit()
        _tell_user(request, admin, k, "key_disabled")
        flash(request, _("Key %(label)s switched off: its expiry date keeps running", label=repr(k.label)))
    return back(f"/admin/users/{user_id}/keys")


@router.post("/users/{user_id}/keys/{key_id}/enable", dependencies=[Depends(verify_csrf)])
def user_key_enable(request: Request, user_id: int, key_id: int, admin: User = Depends(current_admin),
                    db: Session = Depends(get_db)):
    k = _owned_key(db, user_id, key_id)
    if keylib.enable_key(db, k):
        record(db, admin, "key.enable", "print_key", k.id, username=db.get(User, user_id).username, label=k.label)
        db.commit()
        _tell_user(request, admin, k, "key_enabled")
        flash(request, _("Key %(label)s switched on", label=repr(k.label)))
    return back(f"/admin/users/{user_id}/keys")


@router.post("/users/{user_id}/keys/{key_id}/extend", dependencies=[Depends(verify_csrf)])
def user_key_extend(request: Request, user_id: int, key_id: int, validity: str = Form(""), admin: User = Depends(current_admin),
                    db: Session = Depends(get_db)):
    k = _owned_key(db, user_id, key_id)
    owner = db.get(User, user_id)
    try:
        days, once = keylib.parse_duration(owner, validity or str(keylib.default_duration(owner)))
        if once or days is None:
            raise ValueError(_("choose one of the validity periods in the list"))
    except ValueError as exc:
        flash(request, str(exc), "err")
        return back(f"/admin/users/{user_id}/keys")
    if keylib.extend_key(db, k, days):
        record(db, admin, "key.extend", "print_key", k.id, username=owner.username, label=k.label, days=days)
        db.commit()
        _tell_user(request, admin, k, "key_extended")
        flash(request, _("Key %(label)s now expires on %(date)s", label=repr(k.label), date=k.expires_at.strftime("%Y-%m-%d")))
    else:
        flash(request, _("This key can not be extended"), "err")
    return back(f"/admin/users/{user_id}/keys")


# ------------------------------------------------------------------ groups

def _delete_warning(name: str, members: int) -> str:
    if members:
        return ngettext("The group “%(name)s” has %(n)s user: it will be moved to the default group “%(group)s” (with that group's limits). Delete the group?",
                        "The group “%(name)s” has %(n)s users: they will be moved to the default group “%(group)s” (with that group's limits). Delete the group?",
                        members, name=name, group=settings.default_group)
    return _("Delete the group “%(name)s”? It has no users.", name=name)


def _group_row(db: Session, g: Group) -> dict:
    lim = get_limits(db, SubjectType.group, g.id)
    members = db.scalar(select(func.count(User.id)).where(User.group_id == g.id)) or 0
    return {
        "g": g, "members": members, "warning": _delete_warning(g.name, members),
        "jobs": lim[Metric.jobs].limit if Metric.jobs in lim else "",
        "pages": lim[Metric.pages].limit if Metric.pages in lim else "",
        "period": next((r.period.value for r in lim.values()), "weekly"),
    }


@router.get("/groups", response_class=HTMLResponse)
def groups(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    rows = [_group_row(db, g) for g in db.scalars(select(Group).order_by(Group.name))]
    return render(request, "groups.html", admin, rows=rows, system=system_group_names())


@router.get("/groups/new", response_class=HTMLResponse)
async def group_new(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    return render(request, "group_edit.html", admin, g=None, jobs="", pages="", period="weekly",
                  perm=await _permission_form(request, db, None, inherit=False),
                  periods=[p.value for p in Period], system=system_group_names(), admin_group=ADMIN_GROUP, warning="")


@router.get("/groups/{group_id}/edit", response_class=HTMLResponse)
async def group_edit(request: Request, group_id: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    g = db.get(Group, group_id)
    if g is None:
        raise HTTPException(404)
    r = _group_row(db, g)
    perm = await _permission_form(request, db, permissions.get_rule(db, SubjectType.group, g.id), inherit=False)
    return render(request, "group_edit.html", admin, g=g, jobs=r["jobs"], pages=r["pages"], period=r["period"], perm=perm,
                  periods=[p.value for p in Period], system=system_group_names(), admin_group=ADMIN_GROUP,
                  warning=r["warning"])


@router.post("/groups", dependencies=[Depends(verify_csrf)])
def group_create(request: Request, name: str = Form(...), jobs_limit: str = Form(""), pages_limit: str = Form(""),
                 period: str = Form("weekly"), requestable: str = Form(""),
                 perm_color: str = Form(""), perm_one_sided: str = Form(""), perm_printers: str = Form("all"),
                 perm_printer: list[str] = Form([]), perm_hours: str = Form("any"), perm_day: list[str] = Form([]),
                 perm_start: str = Form(""), perm_end: str = Form(""), perm_form: str = Form(""),
                 admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    name = name.strip()
    try:
        jl, pl, per = parse_limit(jobs_limit), parse_limit(pages_limit), Period(period)
        perm = _parse_permissions(False, perm_color, perm_one_sided, perm_printers, perm_printer, perm_hours, perm_day,
                                  perm_start, perm_end) if perm_form else None
    except ValueError as exc:
        flash(request, str(exc), "err")
        return back("/admin/groups/new")
    if not name or len(name) > 64:
        flash(request, _("Invalid group name"), "err")
    elif db.scalars(select(Group).where(Group.name == name)).first():
        flash(request, _("Group %(name)s already exists", name=name), "err")
    else:
        g = Group(name=name, requestable=bool(requestable))
        db.add(g)
        db.flush()
        set_limit(db, SubjectType.group, g.id, Metric.jobs, jl, per)
        set_limit(db, SubjectType.group, g.id, Metric.pages, pl, per)
        perm_changes = _permission_changes(permissions.rule_fields(None), perm) if perm else {}
        if perm_changes:
            permissions.set_rule(db, SubjectType.group, g.id, **perm)
        record(db, admin, "group.create", "group", g.id, name=name, requestable=g.requestable,
               limits={"jobs": jl, "pages": pl, "period": per.value}, **({"permissions": perm_changes} if perm_changes else {}))
        db.commit()
        log.info("admin %s created group %s", admin.username, name)
        flash(request, _("Group %(name)s created", name=name))
        return back("/admin/groups")
    return back("/admin/groups/new")


@router.post("/groups/{group_id}", dependencies=[Depends(verify_csrf)])
def group_update(request: Request, group_id: int, name: str = Form(""), jobs_limit: str = Form(""),
                 pages_limit: str = Form(""), period: str = Form("weekly"), requestable: str = Form(""),
                 perm_color: str = Form(""), perm_one_sided: str = Form(""), perm_printers: str = Form("all"),
                 perm_printer: list[str] = Form([]), perm_hours: str = Form("any"), perm_day: list[str] = Form([]),
                 perm_start: str = Form(""), perm_end: str = Form(""), perm_form: str = Form(""),
                 admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    g = db.get(Group, group_id)
    if g is None:
        raise HTTPException(404)
    edit = f"/admin/groups/{g.id}/edit"
    try:
        jl, pl, per = parse_limit(jobs_limit), parse_limit(pages_limit), Period(period)
        perm = _parse_permissions(False, perm_color, perm_one_sided, perm_printers, perm_printer, perm_hours, perm_day,
                                  perm_start, perm_end) if perm_form else None
    except ValueError as exc:
        flash(request, str(exc), "err")
        return back(edit)
    old_name, new_name = g.name, name.strip()
    if new_name and new_name != old_name:
        if old_name in system_group_names():
            flash(request, _('"%(name)s" is a system group: it can not be renamed', name=old_name), "err")
            return back(edit)
        if len(new_name) > 64:
            flash(request, _("Invalid group name"), "err")
            return back(edit)
        if db.scalars(select(Group).where(Group.name == new_name)).first():
            flash(request, _("Group %(name)s already exists", name=new_name), "err")
            return back(edit)
        g.name = new_name
    g.requestable = bool(requestable)
    set_limit(db, SubjectType.group, g.id, Metric.jobs, jl, per)
    set_limit(db, SubjectType.group, g.id, Metric.pages, pl, per)
    extra = {"renamed_from": old_name} if g.name != old_name else {}
    perm_changes = _permission_changes(permissions.rule_fields(permissions.get_rule(db, SubjectType.group, g.id)), perm) if perm else {}
    if perm_changes:
        permissions.set_rule(db, SubjectType.group, g.id, **perm)
        extra["permissions"] = perm_changes
    record(db, admin, "group.update", "group", g.id, name=g.name, requestable=g.requestable,
           limits={"jobs": jl, "pages": pl, "period": per.value}, **extra)
    db.commit()
    log.info("admin %s updated group %s", admin.username, g.name)
    flash(request, _("Saved %(name)s", name=g.name))
    return back("/admin/groups")


@router.post("/groups/{group_id}/delete", dependencies=[Depends(verify_csrf)])
def group_delete(request: Request, group_id: int, admin: User = Depends(current_admin),
                 db: Session = Depends(get_db)):
    g = db.get(Group, group_id)
    if g is None:
        raise HTTPException(404)
    if g.name in system_group_names():
        flash(request, _('"%(name)s" is a system group: it can not be deleted', name=g.name), "err")
    else:
        # members are not left without a group: they go to the default one (and take its limits)
        members = db.scalars(select(User).where(User.group_id == g.id)).all()
        target = default_group_id(db, settings.default_group) if members else None
        for u in members:
            u.group_id = target
        set_limit(db, SubjectType.group, g.id, Metric.jobs, None)
        set_limit(db, SubjectType.group, g.id, Metric.pages, None)
        permissions.set_rule(db, SubjectType.group, g.id)  # no fields: removes the group's permission rule
        record(db, admin, "group.delete", "group", g.id, name=g.name, moved_users=[u.username for u in members],
               moved_to=settings.default_group if members else None)
        moved, old_name = [u.id for u in members if u.id != admin.id], g.name
        db.delete(g)
        db.commit()
        for uid_ in moved:
            notices.notify_user(request.app.state.sessionmaker, uid_, "group_changed", {"old": old_name, "new": settings.default_group})
        log.info("admin %s deleted group %s (%d user(s) moved to %s)", admin.username, g.name, len(members), settings.default_group)
        flash(request, _("Group %(name)s deleted", name=g.name) + (_("; %(n)s user(s) moved to %(group)s", n=len(members), group=settings.default_group) if members else ""))
    return back("/admin/groups")


# ------------------------------------------------------------------ log of administrative actions

@router.get("/actions", response_class=HTMLResponse)
def actions(request: Request, actor: str = "", action: str = "", page: int = 1,
            admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    page = max(page, 1)
    stmt = select(AdminAudit)
    if actor.strip():
        stmt = stmt.where(AdminAudit.actor_name.contains(actor.strip().lower(), autoescape=True))
    if action.strip():
        stmt = stmt.where(AdminAudit.action.contains(action.strip(), autoescape=True))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(AdminAudit.id.desc()).limit(ACTIONS_PAGE).offset((page - 1) * ACTIONS_PAGE)).all()
    # what the two filter boxes suggest: the administrators, and the actions that really occur in the log
    actors = list(db.scalars(select(User.username).where(User.is_admin).order_by(User.username)))
    action_names = list(db.scalars(select(AdminAudit.action).distinct().order_by(AdminAudit.action)))
    return render(request, "actions.html", admin, rows=rows, total=total, page=page, pages=max(1, -(-total // ACTIONS_PAGE)),
                  f={"actor": actor, "action": action}, actors=actors, action_names=action_names)
