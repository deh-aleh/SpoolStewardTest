"""Administrator switches: maintenance mode, block / hide one printer, and the policy feed for the mDNS announcer."""

import logging
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from spoolsteward.admin.common import back, current_admin, flash, get_db, safe_next, verify_csrf
from spoolsteward import certwatch, notices
from spoolsteward.audit.adminlog import record
from spoolsteward.cups_client import list_queues
from spoolsteward.db.models import User
from spoolsteward.escl import scanners
from spoolsteward.policy import control
from spoolsteward.i18n import N_, _

log = logging.getLogger("spoolsteward.controls")
router = APIRouter()
QUEUE_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,127}$")


@router.post("/admin/maintenance", dependencies=[Depends(verify_csrf)])
def maintenance(request: Request, on: str = Form("1"), next: str = Form("/admin/printers"), notify: str = Form(""),
                admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    """`notify`: the tick in the confirmation window: a notice in the bell of every active account (not the administrator
    who did it). Leaving maintenance also closes the "in maintenance" notices that are still open."""
    flag = on == "1"
    control.set_maintenance(db, flag)
    record(db, admin, "maintenance.on" if flag else "maintenance.off", "gateway", None, notified_users=bool(notify))
    db.commit()
    sm = request.app.state.sessionmaker
    if not flag:
        notices.resolve_users(sm, "maintenance_on", "gateway")
    if notify:
        notices.notify_all_users(sm, "maintenance_on" if flag else "maintenance_off", key="gateway", exclude_user_id=admin.id)
    log.warning("admin %s %s maintenance mode", admin.username, "ENABLED" if flag else "disabled")
    flash(request, _("Gateway in maintenance: printing and scanning stopped for users, printers hidden from the network (within ~30 s)")
          if flag else _("Maintenance over: the gateway is operational again"), "err" if flag else "ok")
    return back(safe_next(next, "/admin/printers"))


def _flag(request: Request, name: str, field: str, on: str, admin: User, db: Session, action_on: str, action_off: str,
          message_on: str, message_off: str):  # the messages are English msgids, translated here
    if not QUEUE_RE.match(name):
        raise HTTPException(404)
    flag = on == "1"
    control.set_printer_flag(db, name, field, flag)
    record(db, admin, action_on if flag else action_off, "printer", None, printer=name)
    db.commit()
    log.warning("admin %s: %s %s", admin.username, action_on if flag else action_off, name)
    flash(request, _(message_on if flag else message_off, name=name), "err" if flag else "ok")
    return back("/admin/printers")


@router.post("/admin/printers/{name}/block", dependencies=[Depends(verify_csrf)])
def printer_block(request: Request, name: str, on: str = Form("1"), admin: User = Depends(current_admin),
                  db: Session = Depends(get_db)):
    return _flag(request, name, "blocked", on, admin, db, "printer.block", "printer.unblock",
                 N_("%(name)s blocked: nobody can print on it, not even the administrators"),
                 N_("%(name)s unblocked: usable again"))


@router.post("/admin/printers/{name}/mdns", dependencies=[Depends(verify_csrf)])
def printer_mdns(request: Request, name: str, on: str = Form("1"), admin: User = Depends(current_admin),
                 db: Session = Depends(get_db)):
    return _flag(request, name, "mdns_hidden", on, admin, db, "printer.mdns_hide", "printer.mdns_show",
                 N_("%(name)s will no longer be announced on the network (mDNS) within ~30 s; it stays usable by whoever knows it"),
                 N_("%(name)s will be announced on the network (mDNS) again within ~30 s"))


@router.get("/mdns/policy", include_in_schema=False)
async def mdns_policy(request: Request):
    """Read by the announcer (another container: no database, and its network cannot reach the printers). Needs the
    shared token when a secret key is configured. Says what not to announce (hidden, unreachable, maintenance) and
    which scanners exist and under which path."""
    if not control.mdns_token_ok(request.headers.get("x-mdns-token", "")):
        raise HTTPException(404)

    def load():
        with request.app.state.sessionmaker() as db:
            return control.mdns_policy(db)

    policy = await run_in_threadpool(load)
    announce = certwatch.parse_announce(request.headers.get("x-mdns-announce", ""))
    if announce:
        request.app.state.mdns_announce = announce
    policy["offline"] = sorted(q["name"].lower() for q in await list_queues(request.app) if not q.get("online", True))
    policy["scanners"] = [{"id": s.id, "name": s.name, "root": s.root} for s in (await scanners(request.app)).values()]
    return JSONResponse(policy, headers={"Cache-Control": "no-store"})
