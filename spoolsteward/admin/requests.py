"""Administrator side of requests and grants."""

import logging
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from spoolsteward import notices
from spoolsteward.timeutil import local_tz, now_utc
from spoolsteward.i18n import _, ngettext
from spoolsteward.admin.common import back, current_admin, flash, get_db, render, verify_csrf
from spoolsteward.cups_client import list_queues
from spoolsteward.db.models import Group, PermissionGrant, QuotaGrant, QuotaRequest, RequestStatus, Resource, User
from spoolsteward.policy import grants, permissions
from spoolsteward.policy.engine import effective_rules
from spoolsteward.policy.ordering import key as queue_key

log = logging.getLogger("spoolsteward.admin")
router = APIRouter(prefix="/admin")


def _what(db: Session, req: QuotaRequest) -> dict:
    p = req.params_json or {}
    if req.kind.value == "quota":
        return {"text": f"+{p.get('amount')} {_(str(p.get('metric')))}", "scope": req.scope.value, "amount": p.get("amount"),
                "metric": p.get("metric")}
    if req.kind.value == "permission":
        return {"text": permissions.describe_grant(str(p.get("perm")), p.get("printer")), "scope": "permanent", "amount": None, "metric": None}
    target = db.get(Group, p.get("target_group_id"))
    return {"text": _("move to group %(name)s", name=target.name if target else "?"), "scope": "permanent", "amount": None, "metric": None}


def _parse_until(duration: str, until: str) -> date | None:
    """The form's "permanent" / "until [date]" choice: None = permanent. A missing or wrong date is an error, never a
    silent permanent grant."""
    if duration != "until":
        return None
    try:
        return date.fromisoformat(until.strip())
    except ValueError:
        raise grants.RequestError(_("choose the last day the permission is valid")) from None


def _grant_state(g: QuotaGrant | PermissionGrant, now: datetime) -> str:
    if g.revoked_at is not None:
        return "revoked"
    until = g.valid_until if g.valid_until is None or g.valid_until.tzinfo else g.valid_until.replace(tzinfo=UTC)
    return "active" if until is None or until > now else "ended"


@router.get("/requests", response_class=HTMLResponse)
async def requests_page(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    """One page for both sides of the same thing: the requests waiting for a decision, the grants they (or an
    administrator directly) produced, and the history."""
    now = now_utc()
    grants.expire_requests(db, now)
    names = {u.id: u.username for u in db.scalars(select(User))}
    pending = db.scalars(select(QuotaRequest).where(QuotaRequest.status == RequestStatus.pending)
                         .order_by(QuotaRequest.id)).all()
    done = db.scalars(select(QuotaRequest).where(QuotaRequest.status != RequestStatus.pending)
                      .order_by(QuotaRequest.id.desc()).limit(50)).all()
    wrap = lambda rs: [{"r": r, "user": names.get(r.user_id, "?"), "by": names.get(r.decided_by), **_what(db, r)} for r in rs]  # noqa: E731
    rows = [{"g": g, "user": names.get(g.user_id, "?"), "state": _grant_state(g, now), "by": names.get(g.granted_by),
             "kind": "quota"} for g in db.scalars(select(QuotaGrant).order_by(QuotaGrant.id.desc()).limit(200))]
    rows += [{"g": g, "user": names.get(g.user_id, "?"), "state": _grant_state(g, now), "by": names.get(g.granted_by),
              "kind": "permission", "what": permissions.describe_grant(g.perm, g.printer), "last_day": grants.last_day(g.valid_until, local_tz())}
             for g in db.scalars(select(PermissionGrant).order_by(PermissionGrant.id.desc()).limit(200))]
    rows.sort(key=lambda x: x["g"].created_at.replace(tzinfo=UTC) if x["g"].created_at.tzinfo is None else x["g"].created_at,
              reverse=True)
    queues = sorted((q["name"] for q in await list_queues(request.app)), key=queue_key)
    return render(request, "requests.html", admin, pending=wrap(pending), done=wrap(done),
                  active=[x for x in rows if x["state"] == "active"], ended=[x for x in rows if x["state"] != "active"],
                  usernames=sorted(u for u in names.values()), queues=queues, today=now.astimezone(local_tz()).date().isoformat())


@router.get("/requests/lacks")
async def lacks(request: Request, username: str, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    """{"perms": [...], "printers": [...], "metrics": [...]}: what this person can still be given, so the "grant without a
    request" form offers only that (a printer they can already use, or colour they already have, is not offered; a quota
    only for the metrics that limit them: with no limit there is nothing to add to). Asked by the page for
    the one user chosen in the form, so the page costs the same with ten users or ten thousand."""
    user = db.scalars(select(User).where(User.username == username)).first()
    if user is None:
        raise HTTPException(404)
    perms = permissions.effective(db, user, now_utc())
    printers = [q for q in sorted((q["name"] for q in await list_queues(request.app)), key=queue_key)
                if not permissions.printer_allowed(perms, q)]
    return {"perms": [p for p, missing in (("color", not perms.color), ("one_sided", not perms.one_sided),
                                           ("hours", perms.window is not None), ("printer", bool(printers))) if missing],
            "printers": printers,
            "metrics": [r.metric.value for r in effective_rules(db, user, Resource.print)]}


def _get(db: Session, req_id: int) -> QuotaRequest:
    req = db.get(QuotaRequest, req_id)
    if req is None:
        raise HTTPException(404)
    return req


@router.post("/requests/{req_id}/approve", dependencies=[Depends(verify_csrf)])
def approve(request: Request, req_id: int, amount: str = Form(""), scope: str = Form(""), note: str = Form(""),
            duration: str = Form("permanent"), until: str = Form(""),
            admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    req = _get(db, req_id)
    try:
        amt = int(amount) if amount.strip().isascii() and amount.strip().isdigit() else None
        if amount.strip() and amt is None:
            raise grants.RequestError("the amount must be a whole number")
        last_day = _parse_until(duration, until) if req.kind.value == "permission" else None
        grant = grants.approve_request(db, req, admin, now_utc(), local_tz(), amount=amt, scope=scope or None, note=note, until=last_day)
        sm = request.app.state.sessionmaker
        if isinstance(grant, PermissionGrant):
            notices.notify_user(sm, req.user_id, "request_approved", {"what": "permission", "perm": grant.perm, "printer": grant.printer,
                                                                      "until": last_day.isoformat() if last_day else None,
                                                                      "note": note.strip()[:200]})
        elif grant is not None:
            notices.notify_user(sm, req.user_id, "request_approved", {"what": "quota", "amount": grant.amount, "metric": grant.metric.value,
                                                                      "scope": grant.scope.value, "note": note.strip()[:200]})
        else:
            group = db.get(Group, req.params_json.get("target_group_id"))
            notices.notify_user(sm, req.user_id, "request_approved", {"what": "group_change", "group": group.name if group else "",
                                                                      "note": note.strip()[:200]})
        flash(request, _("Approved"))
        log.info("admin %s approved request %s", admin.username, req_id)
    except (grants.RequestError, ValueError) as exc:
        flash(request, str(exc), "err")
    return back("/admin/requests")


@router.post("/requests/{req_id}/reject", dependencies=[Depends(verify_csrf)])
def reject(request: Request, req_id: int, note: str = Form(""), admin: User = Depends(current_admin),
           db: Session = Depends(get_db)):
    req = _get(db, req_id)
    try:
        grants.reject_request(db, req, admin, now_utc(), note)
        detail = {"note": note.strip()[:200]}
        if req.kind.value == "permission":
            detail.update(what="permission", perm=(req.params_json or {}).get("perm"), printer=(req.params_json or {}).get("printer"))
        notices.notify_user(request.app.state.sessionmaker, req.user_id, "request_rejected", detail)
        flash(request, _("Rejected"))
    except grants.RequestError as exc:
        flash(request, str(exc), "err")
    return back("/admin/requests")


@router.post("/grants", dependencies=[Depends(verify_csrf)])
def grant_create(request: Request, username: str = Form(""), kind: str = Form("quota"), metric: str = Form(""), amount: str = Form(""),
                 scope: str = Form(""), note: str = Form(""), perm: str = Form(""), printer: str = Form(""),
                 duration: str = Form("permanent"), until: str = Form(""),
                 admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    """Give a quota or a permission without a request. `kind` picks which fields count; a form without JavaScript sends
    all of them, and only the chosen ones are read."""
    user = db.scalars(select(User).where(User.username == username.strip().lower())).first()
    sm = request.app.state.sessionmaker
    try:
        if user is None:
            raise grants.RequestError(_("unknown user"))
        if kind == "permission":
            last_day = _parse_until(duration, until)
            pg = grants.create_permission_grant(db, admin, user, perm, printer, last_day, note, now_utc(), local_tz())
            if user.id != admin.id:
                notices.notify_user(sm, user.id, "grant_given", {"what": "permission", "perm": pg.perm, "printer": pg.printer,
                                                                 "until": last_day.isoformat() if last_day else None,
                                                                 "note": (note or "").strip()[:200]})
        elif kind == "quota":
            n = int(amount) if amount.strip().isascii() and amount.strip().isdigit() else 0
            grant = grants.create_grant(db, admin, user, metric, n, scope, note, now_utc(), local_tz())
            if user.id != admin.id:
                notices.notify_user(sm, user.id, "grant_given",
                                    {"amount": grant.amount, "metric": grant.metric.value, "scope": grant.scope.value, "note": (note or "").strip()[:200]})
        else:
            raise grants.RequestError(_("invalid grant"))
        flash(request, _("Grant created for %(user)s", user=user.username))
    except grants.RequestError as exc:
        flash(request, str(exc), "err")
    return back("/admin/requests")


@router.post("/grants/{grant_id}/revoke", dependencies=[Depends(verify_csrf)])
def grant_revoke(request: Request, grant_id: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    g = db.get(QuotaGrant, grant_id)
    if g is None:
        raise HTTPException(404)
    try:
        user_id, amount, metric = g.user_id, g.amount, g.metric.value
        grants.revoke_grant(db, g, admin, now_utc())
        if user_id != admin.id:
            notices.notify_user(request.app.state.sessionmaker, user_id, "grant_revoked", {"amount": amount, "metric": metric})
        flash(request, _("Grant revoked"))
    except grants.RequestError as exc:
        flash(request, str(exc), "err")
    return back("/admin/requests")


@router.post("/permission-grants/{grant_id}/revoke", dependencies=[Depends(verify_csrf)])
def permission_grant_revoke(request: Request, grant_id: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    g = db.get(PermissionGrant, grant_id)
    if g is None:
        raise HTTPException(404)
    try:
        user_id, perm, printer = g.user_id, g.perm, g.printer
        grants.revoke_permission_grant(db, g, admin, now_utc())
        if user_id != admin.id:
            notices.notify_user(request.app.state.sessionmaker, user_id, "grant_revoked", {"what": "permission", "perm": perm, "printer": printer})
        flash(request, _("Grant revoked"))
    except grants.RequestError as exc:
        flash(request, str(exc), "err")
    return back("/admin/requests")


@router.post("/grants/purge", dependencies=[Depends(verify_csrf)])
def grants_purge(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    """Delete the grants that no longer count (ended or revoked): they only fill the page. The history of who granted what
    stays in the administrators' log."""
    n = grants.purge_ended(db, admin, now_utc())
    flash(request, ngettext("%(n)s ended grant deleted", "%(n)s ended grants deleted", n, n=n) if n else _("No ended grants to delete"))
    return back("/admin/requests")
