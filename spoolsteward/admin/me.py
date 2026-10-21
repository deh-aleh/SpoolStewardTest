"""The signed-in user's own page: consumption, print keys, password, requests for more quota / another group."""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from spoolsteward.timeutil import local_tz, now_utc
from spoolsteward.admin.common import (
    back, client_ip, current_user, flash, get_db, render, session_stamp, verify_csrf,
)
from urllib.parse import quote

from spoolsteward.audit.adminlog import record
from spoolsteward.cups_client import list_queues
from spoolsteward.auth import keys as keylib
from spoolsteward.auth.users import is_emergency
from spoolsteward.auth.passwords import hash_password, validate_new_password, verify_password
from spoolsteward.auth.throttle import USER_FAILS
from spoolsteward import i18n
from spoolsteward.config import settings
from spoolsteward.i18n import N_, _
from spoolsteward.db.models import Group, PrintKey, QuotaRequest, RequestKind, RequestStatus, User
from spoolsteward.policy import grants, permissions

log = logging.getLogger("spoolsteward.me")
THEMES = {"auto": N_("Automatic (as the device)"), "light": N_("Light"), "dark": N_("Dark")}
router = APIRouter(prefix="/me")


def _describe(db: Session, req: QuotaRequest) -> str:
    if req.kind.value == "quota":
        p = req.params_json or {}
        return f"+{p.get('amount')} {_(str(p.get('metric')))} ({_('until the end of the period') if req.scope.value == 'temporary' else _('permanent')})"
    if req.kind.value == "permission":
        p = req.params_json or {}
        return permissions.describe_grant(str(p.get("perm")), p.get("printer"))
    group = db.get(Group, (req.params_json or {}).get("target_group_id"))
    return _("move to group %(name)s", name=group.name if group else "?")


def _waiting_permissions(db: Session, user: User, now: datetime) -> set[str]:
    """The permissions this person asked for and still waits for: "color", "one_sided", "hours", or "printer:<name>"."""
    out = set()
    for r in db.scalars(select(QuotaRequest).where(QuotaRequest.user_id == user.id, QuotaRequest.kind == RequestKind.permission,
                                                   QuotaRequest.status == RequestStatus.pending, QuotaRequest.expires_at > now)):
        p = r.params_json or {}
        out.add(f"printer:{p.get('printer')}" if p.get("perm") == "printer" else str(p.get("perm")))
    return out


@router.get("", response_class=HTMLResponse)
async def me(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    now = now_utc()
    grants.expire_requests(db, now)
    key_rows = db.scalars(select(PrintKey).where(PrintKey.user_id == user.id).order_by(PrintKey.id.desc())).all()
    reqs = db.scalars(select(QuotaRequest).where(QuotaRequest.user_id == user.id).order_by(QuotaRequest.id.desc())
                      .limit(20)).all()
    perms = permissions.effective(db, user, now)
    every_queue = await list_queues(request.app)
    queues = permissions.allowed_queues(perms, every_queue)  # only what this account may print on
    return render(
        request, "me.html", user, perms=perms, can_ask_printer=len(queues) < len(every_queue),
        perm_grants=_grant_lines(permissions.active_grants(db, user, now)), perm_window=permissions.describe_window(perms.window) if perms.window else "",
        in_hours=permissions.in_window(perms.window, now, local_tz()),
        waiting=_waiting_permissions(db, user, now),
        group=db.get(Group, user.group_id) if user.group_id else None,
        options=grants.quota_options(db, user, now, local_tz()),
        active_keys=sum(1 for k in key_rows if keylib.key_is_usable(k, now)),
        requests=[{"r": r, "what": _describe(db, r)} for r in reqs],
        server=request.headers.get("host", ""), ipp_mode=settings.ipp_auth,
        queues=queues,
        can_request_group=not is_emergency(user) and bool(db.scalars(select(Group).where(Group.requestable, Group.id != (user.group_id or 0))).first()),
    )


# ------------------------------------------------------------------ print keys

@router.post("/keys", dependencies=[Depends(verify_csrf)])
async def key_create(request: Request, label: str = Form(""), validity: str = Form(""), user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    try:
        ttl, once = keylib.parse_duration(user, validity or str(keylib.default_duration(user)))
        row, plain = keylib.create_key(db, user, label, ttl, once=once)
    except ValueError as exc:
        flash(request, str(exc), "err")
        return back("/me")
    if user.is_admin:
        record(db, user, "key.create", "print_key", row.id, label=row.label)
        db.commit()
    log.info("print key created user=%s label=%r", user.username, row.label)
    expires = (row.expires_at.astimezone(local_tz()).strftime("%Y-%m-%d %H:%M") if row.single_use else row.expires_at.strftime("%Y-%m-%d")) if row.expires_at else _("never")
    host = request.headers.get("host", "")
    commands = [
        {"queue": q["name"],
         "url": f'ipps://{quote(user.username, safe="")}:{plain}@{host}/printers/{q["name"]}',
         "cmd": f'lpadmin -p {q["name"]} -E -v "ipps://{quote(user.username, safe="")}:{plain}@{host}/printers/{q["name"]}" '
                f'-m everywhere -o auth-info-required=none'}
        for q in permissions.allowed_queues(permissions.effective(db, user, now_utc()), await list_queues(request.app))
    ]
    return render(request, "secret_shown.html", user, title=_("New print key"), what=_("Print key"), secret=plain,
                  commands=commands,
                  note=_("Shown only now. Use it as the PASSWORD when your computer or phone asks to sign in to the printer; "
                         "the user name is %(user)s. It expires on %(date)s and works only for printing. "
                         "If you lose it or lose the device, revoke it here and create another.", user=user.username, date=expires)
                       if not row.single_use else
                       _("Shown only now. Use it as the PASSWORD when your computer or phone asks to sign in to the printer; "
                         "the user name is %(user)s. It works for ONE print only, and only until %(date)s if nobody uses it. "
                         "Create another one for the next print.", user=user.username, date=expires),
                  next_url="/me/keys")


@router.get("/keys", response_class=HTMLResponse)
def keys_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    now = now_utc()
    rows = db.scalars(select(PrintKey).where(PrintKey.user_id == user.id).order_by(PrintKey.id.desc())).all()
    return render(request, "keys.html", user, owner=user, keys=keylib.listing(rows, now),
                  choices=keylib.duration_choices(user), default_choice=str(keylib.default_duration(user)),
                  base="/me/keys", own=True, server=request.headers.get("host", ""), ipp_mode=settings.ipp_auth)


@router.post("/keys/revoke-all", dependencies=[Depends(verify_csrf)])
def key_revoke_all(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    n = keylib.revoke_all(db, user)
    flash(request, _("%(n)s key(s) revoked", n=n))
    return back("/me/keys")


@router.post("/keys/{key_id}/revoke", dependencies=[Depends(verify_csrf)])
def key_revoke(request: Request, key_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(PrintKey, key_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404)
    if keylib.revoke_key(db, row):
        flash(request, _("Key %(label)s revoked", label=repr(row.label)))
    return back("/me/keys")


# ------------------------------------------------------------------ password (local accounts)

def _pw_page(user: User) -> str:
    return "/me/password" if user.must_change_password else "/me/settings"


@router.get("/password", response_class=HTMLResponse)
def password_form(request: Request, user: User = Depends(current_user)):
    if user.provider != "local":
        flash(request, _("Your password is managed by RADIUS, not here"), "err")
        return back("/me")
    if not user.must_change_password:
        return back("/me/settings")  # the form lives in the settings tab
    return render(request, "password.html", user, forced=True)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(current_user)):
    return render(request, "settings.html", user, themes=THEMES, refresh_min=REFRESH_MIN, refresh_max=REFRESH_MAX,
                  secondary_max=SECONDARY_MAX)


@router.post("/settings/theme", dependencies=[Depends(verify_csrf)])
def settings_theme(request: Request, theme: str = Form("auto"), user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    if theme not in THEMES:
        raise HTTPException(400, "unknown theme")
    user.theme = theme
    db.commit()
    flash(request, _("Theme saved"))
    return back("/me/settings")


@router.post("/settings/language", dependencies=[Depends(verify_csrf)])
def settings_language(request: Request, language: str = Form(""), user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    """The interface language of this user; empty = follow the browser."""
    if language and language not in i18n.LANGUAGES:
        raise HTTPException(400, "unknown language")
    user.language = language or None
    db.commit()
    request.session["lang"] = language  # the session carries it, so no request needs the database to know it
    i18n.set_language(language or i18n.negotiate(request.headers.get("accept-language")))  # this reply already speaks it
    flash(request, _("Language saved"))
    return back("/me/settings")


REFRESH_MIN, REFRESH_MAX = 5, 3600
SECONDARY_MAX = 7 * 24 * 60   # minutes (a week)


@router.post("/settings/notices", dependencies=[Depends(verify_csrf)])
def settings_notices(request: Request, minutes: str = Form("20"), user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    """Minutes after which secondary notices are marked as read by themselves; 0 = never."""
    raw = minutes.strip()
    if not (raw.isascii() and raw.isdigit()) or int(raw) > SECONDARY_MAX:
        flash(request, _("Enter 0 (never) or a number of minutes up to %(max)s", max=SECONDARY_MAX), "err")
        return back("/me/settings")
    user.secondary_read_minutes = int(raw)
    db.commit()
    flash(request, _("Secondary notices are not read automatically") if user.secondary_read_minutes == 0
          else _("Secondary notices are read automatically after %(n)s minutes", n=user.secondary_read_minutes))
    return back("/me/settings")


@router.post("/settings/refresh", dependencies=[Depends(verify_csrf)])
def settings_refresh(request: Request, seconds: str = Form("30"), user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    """Seconds between automatic reloads of the audit page (administrators); 0 switches it off."""
    raw = seconds.strip()
    if not (raw.isascii() and raw.isdigit()) or not (int(raw) == 0 or REFRESH_MIN <= int(raw) <= REFRESH_MAX):
        flash(request, _("Enter 0 (off) or a number of seconds between %(min)s and %(max)s", min=REFRESH_MIN, max=REFRESH_MAX), "err")
        return back("/me/settings")
    user.refresh_seconds = int(raw)
    db.commit()
    flash(request, _("Automatic refresh off") if user.refresh_seconds == 0
          else _("Automatic refresh every %(n)s seconds", n=user.refresh_seconds))
    return back("/me/settings")


@router.post("/password", dependencies=[Depends(verify_csrf)])
def password_change(request: Request, current: str = Form(""), new: str = Form(""), confirm: str = Form(""),
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.provider != "local":
        raise HTTPException(404)
    throttle = request.app.state.throttle
    key = f"pw|{user.id}|{client_ip(request)}"
    if throttle.blocked(key, USER_FAILS):
        flash(request, _("Too many attempts, retry later"), "err")
        return back(_pw_page(user))
    if not verify_password(user.password_hash, current):
        throttle.fail(key)
        flash(request, _("The current password is wrong"), "err")
        return back(_pw_page(user))
    error = (_("the two new passwords differ") if new != confirm else
             _("the new password must differ from the current one") if new == current else
             validate_new_password(new, user.username))
    if error:
        flash(request, error, "err")
        return back(_pw_page(user))
    forced = user.must_change_password
    user.password_hash, user.must_change_password = hash_password(new), False
    user.password_changed_at = now_utc()
    if user.is_admin:
        record(db, user, "password.change", "user", user.id)
    db.commit()
    request.session["stamp"] = session_stamp(user)  # this session survives; every other one ends
    flash(request, _("Password changed"))
    return back(("/admin/" if user.is_admin else "/me") if forced else "/me/settings")


# ------------------------------------------------------------------ requests

@router.get("/request/quota", response_class=HTMLResponse)
def quota_form(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "request_quota.html", user, options=grants.quota_options(db, user, now_utc(), local_tz()),
                  max_open=settings.max_open_requests, reason_required=settings.request_reason_required)


@router.post("/request/quota", dependencies=[Depends(verify_csrf)])
def quota_submit(request: Request, metric: str = Form(""), amount: str = Form(""), scope: str = Form(""),
                 reason: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    # no "period" field exists: the period is the one the administrator set for this user
    try:
        n = int(amount) if amount.strip().isascii() and amount.strip().isdigit() else 0
        grants.create_quota_request(db, user, metric, n, scope, reason, now_utc())
    except grants.RequestError as exc:
        flash(request, str(exc), "err")
        return back("/me/request/quota")
    flash(request, _("Request sent: an administrator will answer it"))
    return back("/me")


@router.get("/request/group", response_class=HTMLResponse)
def group_form(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    options = db.scalars(select(Group).where(Group.requestable, Group.id != (user.group_id or 0))
                         .order_by(Group.name)).all()
    return render(request, "request_group.html", user, options=options, reason_required=settings.request_reason_required,
                  current=db.get(Group, user.group_id) if user.group_id else None)


@router.post("/request/group", dependencies=[Depends(verify_csrf)])
def group_submit(request: Request, group_id: int = Form(0), reason: str = Form(""),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        grants.create_group_request(db, user, group_id, reason, now_utc())
    except grants.RequestError as exc:
        flash(request, str(exc), "err")
        return back("/me/request/group")
    flash(request, _("Request sent: an administrator will answer it"))
    return back("/me")


def _grant_lines(rows) -> list[dict]:
    """The permissions granted to this person, for the list on their page: what, and the last day (None = permanent)."""
    return [{"what": permissions.describe_grant(g.perm, g.printer), "until": grants.last_day(g.valid_until, local_tz())} for g in rows]


@router.get("/request/permission", response_class=HTMLResponse)
async def permission_form(request: Request, perm: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Only what this person lacks is offered. `perm` (and `printer`) in the address pre-select the choice: the links on /me use them."""
    perms = permissions.effective(db, user, now_utc())
    every_queue = await list_queues(request.app)
    blocked = [q["name"] for q in every_queue if not permissions.printer_allowed(perms, q["name"])]
    waiting = _waiting_permissions(db, user, now_utc())  # already asked: not offered again until the answer comes
    blocked = [n for n in blocked if f"printer:{n.lower()}" not in waiting]
    options = [(p, label) for p, label, lacks in (
        ("color", _("colour printing"), not perms.color), ("one_sided", _("one-sided printing"), not perms.one_sided),
        ("hours", _("printing outside the hours"), perms.window is not None), ("printer", _("a printer"), bool(blocked)))
        if lacks and p not in waiting]
    return render(request, "request_permission.html", user, options=options, blocked=blocked, max_open=settings.max_open_requests,
                  reason_required=settings.request_reason_required,
                  chosen=perm if perm in dict(options) else "", chosen_printer=request.query_params.get("printer", ""))


@router.post("/request/permission", dependencies=[Depends(verify_csrf)])
async def permission_submit(request: Request, perm: str = Form(""), printer: str = Form(""), reason: str = Form(""),
                            user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        name = None
        if perm == "printer":  # only a printer that exists: a made-up name would just sit in a request
            name = next((q["name"] for q in await list_queues(request.app) if q["name"].lower() == printer.strip().lower()), None)
            if name is None:
                raise grants.RequestError(_("that printer does not exist"))
        grants.create_permission_request(db, user, perm, name, reason, now_utc())
    except grants.RequestError as exc:
        flash(request, str(exc), "err")
        return back("/me/request/permission")
    flash(request, _("Request sent: an administrator will answer it"))
    return back("/me")


@router.post("/requests/{req_id}/cancel", dependencies=[Depends(verify_csrf)])
def request_cancel(request: Request, req_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    req = db.get(QuotaRequest, req_id)
    if req is None or req.user_id != user.id:
        raise HTTPException(404)
    try:
        grants.cancel_request(db, req, user, now_utc())
        flash(request, _("Request canceled"))
    except grants.RequestError as exc:
        flash(request, str(exc), "err")
    return back("/me")
