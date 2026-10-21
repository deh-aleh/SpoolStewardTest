"""Update from the portal. Allowed only when a scheduled backup is working, and only if the optional updater container runs.
A beta or an update with breaking changes needs one more step: the administrator ticks the box of the confirmation."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from spoolsteward import updates
from spoolsteward.admin.common import back, current_admin, flash, get_db, verify_csrf
from spoolsteward.audit.adminlog import record
from spoolsteward.backup import service
from spoolsteward.db.models import User
from spoolsteward.i18n import _

log = logging.getLogger("spoolsteward.updates")
router = APIRouter(prefix="/admin/update")


def panel(request: Request, db: Session) -> dict:
    """Everything the Backup page needs to draw the update card, including why the button may be off."""
    info = getattr(request.app.state, "update_info", None)
    alive = updates.updater_alive()
    target = updates.eligible_backup(db, datetime.now(UTC))
    if info is None:
        reason = "none"
    elif info.get("major"):
        reason = "major"   # another major version is installed by hand, with scripts/update.sh
    elif not alive:
        reason = "no_updater"
    elif target is None:
        reason = "no_backup"
    elif updates.pending_request():
        reason = "busy"
    else:
        reason = ""
    return {"info": info, "updater": alive, "backup": target, "reason": reason, "status": updates.read_status(),
            "accept": bool(info and (info.get("beta") or info.get("breaking")))}


@router.post("", dependencies=[Depends(verify_csrf)])
async def start(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    p = panel(request, db)
    if p["reason"]:
        flash(request, _("The update cannot start now: see the Update box on this page"), "err")
        return back("/admin/backups")
    form = await request.form()
    if form.get("version", p["info"]["version"]) != p["info"]["version"]:   # a newer check changed the offer meanwhile
        flash(request, _("The update was not started: the version on offer has changed. Look at the Update box again."), "err")
        return back("/admin/backups")
    if p["accept"] and form.get("accept") != "1":
        flash(request, _("The update was not started: tick the box in the confirmation to accept the warning."), "err")
        return back("/admin/backups")
    # a fresh copy first: the one that makes the update acceptable may be a day old
    ok, message = await run_in_threadpool(service.run_target, request.app.state.sessionmaker, p["backup"].id, prune=False)
    if not ok:
        flash(request, _("The update was not started because the backup failed: %(error)s", error=message), "err")
        return back("/admin/backups")
    version = p["info"]["version"]
    try:
        await run_in_threadpool(updates.write_request, version, admin.username, message.split(" ")[0])
    except (ValueError, OSError) as exc:
        log.error("update request not written: %s", exc)
        flash(request, _("The update could not be handed to the updater: %(error)s", error=str(exc)), "err")
        return back("/admin/backups")
    record(db, admin, "update.request", "update", 0, version=version, beta=bool(p["info"].get("beta")),
           breaking=bool(p["info"].get("breaking")))
    db.commit()
    flash(request, _("Update to %(version)s requested. The gateway restarts in a minute or two; this page shows the result.", version=version))
    return back("/admin/backups")
