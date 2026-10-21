"""The bell of every user: what an administrator did that concerns them (a key revoked, disabled, extended)."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from spoolsteward.admin.common import back, current_user, flash, get_db, render, verify_csrf
from spoolsteward.db.models import Notice, User
from spoolsteward.i18n import _

router = APIRouter(prefix="/me/notices")


@router.get("", response_class=HTMLResponse)
def listing(request: Request, all: int = 0, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.is_admin:   # administrators have one bell: their own notices are at the top of the administrators' page
        return back("/admin/notices" + ("?all=1" if all else ""))
    q = select(Notice).where(Notice.user_id == user.id).order_by(Notice.last_at.desc())
    if not all:
        q = q.where(Notice.dismissed_at.is_(None))
    user.own_notices_seen_at = datetime.now(UTC)   # opening the list is what "seeing" them means
    db.commit()
    return render(request, "me_notices.html", user, own_rows=list(db.scalars(q.limit(200))), show_all=bool(all))


@router.post("/{nid}/dismiss", dependencies=[Depends(verify_csrf)])
def dismiss(request: Request, nid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    n = db.get(Notice, nid)
    if n is None or n.user_id != user.id:   # somebody else's notice looks like one that does not exist
        raise HTTPException(404)
    if n.dismissed_at is None:
        n.dismissed_at, n.dismissed_by = datetime.now(UTC), user.username
        db.commit()
    return back("/me/notices")


@router.post("/dismiss-all", dependencies=[Depends(verify_csrf)])
def dismiss_all(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    now, done = datetime.now(UTC), 0
    for n in db.scalars(select(Notice).where(Notice.user_id == user.id, Notice.dismissed_at.is_(None))):
        n.dismissed_at, n.dismissed_by = now, user.username
        done += 1
    db.commit()
    flash(request, _("%(n)s notices dismissed", n=done))
    return back("/me/notices")
