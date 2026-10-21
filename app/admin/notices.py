"""The bell: notices for administrators (things the gateway noticed and nobody asked about)."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import notices
from app.admin.common import back, current_admin, flash, get_db, render, verify_csrf
from app.audit.adminlog import record
from app.db.models import Notice, User
from app.i18n import _, ngettext

router = APIRouter(prefix="/admin/notices")


def _every(seconds: int) -> str:
    if seconds < 3600:
        return ngettext("%(n)s minute", "%(n)s minutes", max(seconds // 60, 1))
    if seconds < 86400:
        return ngettext("%(n)s hour", "%(n)s hours", seconds // 3600)
    return ngettext("%(n)s day", "%(n)s days", seconds // 86400)


@router.get("", response_class=HTMLResponse)
def listing(request: Request, all: int = 0, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    q = select(Notice).where(Notice.user_id.is_(None)).order_by(Notice.last_at.desc())
    if not all:
        q = q.where(Notice.dismissed_at.is_(None))
    own = select(Notice).where(Notice.user_id == admin.id).order_by(Notice.last_at.desc())   # the administrator's own notices share this page: one bell
    if not all:
        own = own.where(Notice.dismissed_at.is_(None))
    own_rows = list(db.scalars(own.limit(200)))
    admin.notices_seen_at = admin.own_notices_seen_at = datetime.now(UTC)   # opening the list is what "seeing" them means: the red dot goes away
    db.commit()
    rows = [{"n": n, "extra": notices.pending(n.kind, n.key) if n.dismissed_at is None else 0} for n in db.scalars(q.limit(200))]
    return render(request, "notices.html", admin, rows=rows, own_rows=own_rows, show_all=bool(all), when=_every(notices.interval()))


@router.post("/{nid}/dismiss", dependencies=[Depends(verify_csrf)])
def dismiss(request: Request, nid: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    n = db.get(Notice, nid)
    if n is None or n.user_id is not None:
        raise HTTPException(404)
    if n.dismissed_at is None:
        n.dismissed_at, n.dismissed_by = datetime.now(UTC), admin.username
        record(db, admin, "notice.dismiss", "notice", n.id, kind=n.kind, source=n.key)
        db.commit()
    return back("/admin/notices")


@router.post("/dismiss-all", dependencies=[Depends(verify_csrf)])
def dismiss_all(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    now, done = datetime.now(UTC), 0
    for n in db.scalars(select(Notice).where(Notice.dismissed_at.is_(None), Notice.user_id.is_(None))):
        n.dismissed_at, n.dismissed_by = now, admin.username
        done += 1
    if done:
        record(db, admin, "notice.dismiss_all", "notice", 0, count=done)
        db.commit()
    flash(request, _("%(n)s notices dismissed", n=done))
    return back("/admin/notices")
