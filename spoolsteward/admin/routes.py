"""Portal: login, first-run setup, audit of print jobs. Server-rendered pages, POST-redirect-GET, no JS needed."""

import csv
import hmac
import io
import logging
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from spoolsteward.i18n import _
from spoolsteward.admin.common import (
    back, client_ip, current_admin, current_user, flash, get_db, localtime, render, session_stamp, verify_csrf,
)
from spoolsteward.admin.audit_view import audit_row
from spoolsteward.audit.adminlog import record
from spoolsteward.auth import service
from spoolsteward.auth.passwords import hash_password, validate_new_password
from spoolsteward.auth.throttle import IP_FAILS, USER_FAILS, register_failure
from spoolsteward.config import settings
from spoolsteward.db.models import AuditArchive, JobLog, JobState, User
from spoolsteward.policy import control
from spoolsteward.escl import scanner_device
from spoolsteward.ipp import codec

log = logging.getLogger("spoolsteward.admin")
router = APIRouter(prefix="/admin")
auth_router = APIRouter()  # /login, /logout, /setup: for every user, not only admins
JOBS_PAGE = 30  # audit rows per page


# ------------------------------------------------------------------ login / logout

@auth_router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login.html", setup_pending=bool(getattr(request.app.state, "setup_code", None)),
                  setup_user=settings.bootstrap_admin_username)


@auth_router.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    if request.url.scheme != "https" and not settings.allow_insecure_auth:
        raise HTTPException(403, "TLS required")
    throttle = request.app.state.throttle
    ip = client_ip(request)
    ukey, ikey = f"u:{username.strip().lower()}|{ip}", f"ip:{ip}"
    if throttle.blocked(ukey, USER_FAILS) or throttle.blocked(ikey, IP_FAILS):
        flash(request, _("Too many failed attempts, retry later"), "err")
        return back("/login")
    user = await service.portal_login(request.app, username, password)
    if user is None:
        register_failure(throttle, ukey, ikey, password)
        log.warning("portal login failed user=%r ip=%s", username, ip)
        flash(request, _("Invalid credentials"), "err")
        return back("/login")
    throttle.clear(ukey)
    request.session.clear()  # new session on login
    request.session.update(uid=user.id, csrf=secrets.token_urlsafe(32), stamp=session_stamp(user),
                           lang=user.language or "")  # "" = follow the browser
    log.info("portal login user=%s admin=%s ip=%s", user.username, user.is_admin, ip)
    if user.is_admin:
        def note():
            with request.app.state.sessionmaker() as db:
                record(db, db.get(User, user.id), "login", "user", user.id, ip=ip)
                db.commit()
        await run_in_threadpool(note)
    if user.must_change_password:
        return back("/me/password")
    return back("/admin/" if user.is_admin else "/me")


@auth_router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return back("/login")


@router.get("/")
def index(user: User = Depends(current_user)):
    # an anonymous visitor is sent to the login page by current_user itself
    return back("/admin/jobs" if user.is_admin else "/me")


# ------------------------------------------------------------------ first-run setup

def _setup_pending(request: Request) -> bool:
    return bool(getattr(request.app.state, "setup_code", None))


@auth_router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request):
    if not _setup_pending(request):
        raise HTTPException(404)
    return render(request, "setup.html", username=settings.bootstrap_admin_username)


@auth_router.post("/setup")
async def setup(request: Request, code: str = Form(""), password: str = Form(""), confirm: str = Form("")):
    if request.url.scheme != "https" and not settings.allow_insecure_auth:
        raise HTTPException(403, "TLS required")
    if not _setup_pending(request):
        raise HTTPException(404)
    throttle = request.app.state.throttle
    ip = client_ip(request)
    key = f"setup|{ip}"
    if throttle.blocked(key, USER_FAILS):
        flash(request, _("Too many wrong codes, retry later"), "err")
        return back("/setup")
    if not hmac.compare_digest(code.strip(), request.app.state.setup_code):
        throttle.fail(key)
        log.warning("first-run setup: wrong code from %s", ip)
        flash(request, _("Wrong code. It is in the portal log (docker compose logs gateway)"), "err")
        return back("/setup")
    name = settings.bootstrap_admin_username
    error = None if password == confirm else _("the two passwords differ")
    error = error or validate_new_password(password, name)
    if error:
        flash(request, error, "err")
        return back("/setup")

    def apply() -> bool:
        with request.app.state.sessionmaker() as db:
            user = db.scalars(select(User).where(User.username == name)).first()
            if user is None or user.provider != "local" or user.password_hash:
                return False
            user.password_hash, user.must_change_password = hash_password(password), False
            user.password_changed_at = datetime.now(UTC)
            record(db, user, "admin.setup", "user", user.id, ip=ip)
            db.commit()
            return True

    if not await run_in_threadpool(apply):
        raise HTTPException(409, "already configured")
    request.app.state.setup_code = None  # one use only
    log.info("first-run setup completed for %s from %s", name, ip)
    flash(request, _("Password set. Log in as %(name)s.", name=name))
    return back("/login")


# ------------------------------------------------------------------ print audit

def _job_query(user: str, state: str, q: str):
    # the name stored on the job row survives the deletion of the account (user_id is then NULL)
    who = func.coalesce(User.username, JobLog.user_name)
    stmt = select(JobLog, who).join(User, User.id == JobLog.user_id, isouter=True)
    if user.strip():
        stmt = stmt.where(who.contains(user.strip().lower(), autoescape=True))
    if state in {s.value for s in JobState}:
        stmt = stmt.where(JobLog.state == JobState(state))
    return stmt


TITLE_SCAN = 5000  # the file names are encrypted in the database: searching them means opening rows, so only the latest ones


def _name_matches(j: JobLog, q: str) -> bool:
    q = q.strip().lower()
    return q in (j.title or "").lower() or q in str((j.options_json or {}).get("document_name") or "").lower()


@router.get("/jobs", response_class=HTMLResponse)
async def jobs(request: Request, user: str = "", state: str = "", q: str = "", page: int = 1,
               admin: User = Depends(current_admin)):
    page = max(page, 1)
    scanner = await scanner_device(request.app)

    def load():
        with request.app.state.sessionmaker() as db:
            stmt = _job_query(user, state, q)
            if q.strip():  # by file name: open the latest rows and keep the ones that match
                found = [r for r in db.execute(stmt.order_by(JobLog.id.desc()).limit(TITLE_SCAN)) if _name_matches(r[0], q)]
                total, rows = len(found), found[(page - 1) * JOBS_PAGE:page * JOBS_PAGE]
            else:
                total = db.scalar(select(func.count()).select_from(stmt.subquery()))
                rows = db.execute(stmt.order_by(JobLog.id.desc()).limit(JOBS_PAGE).offset((page - 1) * JOBS_PAGE)).all()
            archives = db.execute(select(func.count(AuditArchive.id), func.min(AuditArchive.first_ts),
                                         func.max(AuditArchive.last_ts)).where(AuditArchive.source_table == "job_log")).one()
            return total, rows, tuple(archives), control.titles_enabled(db)

    total, rows, archives, titles_on = await run_in_threadpool(load)
    return render(request, "jobs.html", admin, rows=[audit_row(j, name, scanner) for j, name in rows], total=total,
                  page=page, pages=max(1, -(-total // JOBS_PAGE)), f={"user": user, "state": state, "q": q},
                  states=[s.value for s in JobState], archives=archives if archives[0] else None,
                  refresh=admin.refresh_seconds, titles_on=titles_on, title_scan=TITLE_SCAN)


def _csv_safe(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s  # spreadsheet formula injection


@router.get("/jobs.csv")
async def jobs_csv(request: Request, user: str = "", state: str = "", q: str = "", admin: User = Depends(current_admin)):
    scanner = await scanner_device(request.app)

    def load():
        with request.app.state.sessionmaker() as db:
            return [audit_row(j, name, scanner) for j, name in db.execute(_job_query(user, state, q).order_by(JobLog.id.desc()))
                    if not q.strip() or _name_matches(j, q)]

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["n", "time", "user", "kind", "state", "printer", "title", "pages", "cups_job_id", "reason",
                "adf", "color", "sides"])
    for r in await run_in_threadpool(load):
        j, specs = r["j"], [v for _label, v in r["specs"]]  # [colour, sides] for a print, [] for a scan
        w.writerow([j.id, localtime(j.ts), _csv_safe(r["name"]), j.kind.value, j.state.value, _csv_safe(r["device"]),
                    _csv_safe(j.title), j.pages or "", j.cups_job_id or "", _csv_safe(j.denied_reason),
                    "adf" if r["adf"] else "", specs[0] if specs else "", specs[1] if specs else ""])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="print-audit.csv"'})


@router.post("/jobs/titles", dependencies=[Depends(verify_csrf)])
def job_titles(request: Request, on: str = Form("1"), admin: User = Depends(current_admin),
               db: Session = Depends(get_db)):
    """Start or stop saving the names of the files of new prints and scans (the names already saved stay)."""
    flag = on == "1"
    control.set_titles(db, flag)
    record(db, admin, "jobtitles.on" if flag else "jobtitles.off", "gateway", None)
    db.commit()
    log.warning("admin %s %s saving the file names of new jobs", admin.username, "STARTED" if flag else "STOPPED")
    flash(request, _("From now on the names of new files are saved") if flag
          else _("From now on the names of new files are not saved"), "ok" if flag else "err")
    return back("/admin/jobs")


# ------------------------------------------------------------------ admin: cancel a queued job

def _load_job(sm, log_id: int):
    with sm() as db:
        row = db.get(JobLog, log_id)
        owner = db.get(User, row.user_id) if row and row.user_id else None
        return (row.printer, row.cups_job_id, row.state, owner.username if owner else None) if row else None


@router.post("/jobs/{log_id}/cancel", dependencies=[Depends(verify_csrf)])
async def job_cancel(request: Request, log_id: int, admin: User = Depends(current_admin)):
    """Cancel-Job as the job's owner: CUPS' own @OWNER policy accepts it, so the gateway needs no extra privilege.
    The audit row is not touched here; the reconciler records what CUPS reports."""
    sm = request.app.state.sessionmaker
    info = await run_in_threadpool(_load_job, sm, log_id)
    if info is None:
        raise HTTPException(404)
    printer, cups_id, state, owner = info
    if state is not JobState.accepted or not (printer and cups_id and owner):
        flash(request, _("That job is not waiting in a queue"), "err")
        return back("/admin/jobs")
    req = codec.make_request(0x0008, 1, printer_uri=f"ipp://localhost/printers/{printer}",
                             requesting_user_name=settings.cups_user_prefix + owner)
    req.group(codec.TAG_OPERATION).attrs.append(codec.Attr("job-id", [codec.Val(codec.TAG_INTEGER, cups_id)]))
    try:
        resp = await request.app.state.upstream.post(
            f"/printers/{printer}", content=codec.encode(req),
            headers={"Content-Type": "application/ipp", "Host": "localhost"})
        ok = resp.headers.get("content-type", "").startswith("application/ipp") and codec.decode(resp.content).code < 0x100
    except Exception:
        log.exception("cancel of job %s failed", log_id)
        ok = False

    def note():
        with sm() as db:
            record(db, db.get(User, admin.id), "job.cancel" if ok else "job.cancel_failed", "job_log", log_id,
                   owner=owner, printer=printer, cups_job_id=cups_id)
            db.commit()
    await run_in_threadpool(note)
    flash(request, _("Cancel requested") if ok else _("CUPS did not accept the cancel (already printed?)"), "ok" if ok else "err")
    return back("/admin/jobs")
