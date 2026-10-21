"""Backups in the portal: every administrator can set up their own destination, encryption and retention; the
general backup of .env is shown beside them (read-only: it is changed in the file)."""

import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.i18n import _
from app.admin.updates import panel as update_panel
from app.admin.common import back, current_admin, flash, get_db, render, verify_csrf
from app.audit.adminlog import record
from app.backup import core, crypto, service
from app.config import settings
from app.db.models import BackupTarget, User

log = logging.getLogger("app.backup")
router = APIRouter(prefix="/admin/backups")
MODES = ("none", "passphrase", "key")


def _can_edit(t: BackupTarget, admin: User) -> bool:
    return not t.is_global and (t.owner_id == admin.id or t.owner_id is None)


def _get(db: Session, tid: int) -> BackupTarget:
    t = db.get(BackupTarget, tid)
    if t is None:
        raise HTTPException(404)
    return t


def _int(raw: str, low: int, high: int, label: str) -> int:
    if not (raw.strip().isascii() and raw.strip().isdigit() and low <= int(raw) <= high):
        raise ValueError(_("%(label)s must be a number between %(low)s and %(high)s", label=label, low=low, high=high))
    return int(raw)


def _common(name: str, folder: str, hour: str, keep_daily: str, keep_weekly: str, keep_monthly: str, require_marker: bool = False) -> dict:
    name = name.strip()
    if not 1 <= len(name) <= 80:
        raise ValueError(_("Give the backup a name (up to 80 characters)"))
    try:
        path = core.bounded(core.resolve_folder, folder, timeout=20)  # also proves the gateway can write there
        if require_marker:
            core.bounded(core.make_marker, path, timeout=20)  # the administrator says: this folder is mounted right now
    except core.BackupError as exc:
        raise ValueError(str(exc)) from exc
    return dict(name=name, folder=folder.strip().rstrip("/"), require_marker=require_marker, hour=_int(hour, 0, 23, _("Hour")),
                keep_daily=_int(keep_daily, 1, 365, _("Daily copies")), keep_weekly=_int(keep_weekly, 0, 520, _("Weekly copies")),
                keep_monthly=_int(keep_monthly, 0, 240, _("Monthly copies")))


def _folders() -> list[str]:
    try:
        return core.bounded(core.existing_folders)
    except core.BackupError:
        return []


def _row(db: Session, t: BackupTarget, admin: User, instance: str, now: datetime, dead: list) -> dict:
    """`dead` collects the first disk problem of this page view: once a disk did not answer, the others are not tried
    (each try would cost the full wait)."""
    files, problem = [], None
    try:
        if dead:
            raise core.BackupError(dead[0])
        files = core.bounded(lambda: core.list_backups(core.resolve_folder(t.folder, create=False), instance, t.id))
    except core.BackupError as exc:
        problem = str(exc)
        if not dead and isinstance(exc, core.DiskNotAnswering):
            dead.append(problem)
    return {"t": t, "files": files[:8], "count": len(files), "problem": problem, "editable": _can_edit(t, admin),
            "health": service.health(t, now), "mine": t.owner_id == admin.id}


@router.get("", response_class=HTMLResponse)
def listing(request: Request, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    service.sync_global(db)
    instance, now = service.instance_id(db), datetime.now(UTC)
    targets = db.scalars(select(BackupTarget).order_by(BackupTarget.is_global.desc(), BackupTarget.id)).all()
    dead: list[str] = []
    rows = [_row(db, t, admin, instance, now, dead) for t in targets]
    return render(request, "backups.html", admin, rows=rows,
                  root=settings.backup_root, host_root=settings.backup_host_path, folders=[] if dead else _folders(),
                  root_ok=Path(settings.backup_root).is_dir(), min_pass=crypto.MIN_PASSPHRASE, instance=instance,
                  defaults=dict(hour=3, keep_daily=7, keep_weekly=4, keep_monthly=12),
                  upd=update_panel(request, db))


@router.post("", dependencies=[Depends(verify_csrf)])
def create(request: Request, name: str = Form(""), folder: str = Form(""), hour: str = Form("3"),
           keep_daily: str = Form("7"), keep_weekly: str = Form("4"), keep_monthly: str = Form("12"),
           mode: str = Form("none"), require_marker: str = Form(""), passphrase: str = Form(""), passphrase2: str = Form(""), recipient: str = Form(""),
           admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    try:
        if mode not in MODES:
            raise ValueError(_("Unknown encryption mode"))
        fields = _common(name, folder, hour, keep_daily, keep_weekly, keep_monthly, bool(require_marker))
        bundle = None
        if mode == "passphrase":
            if passphrase != passphrase2:
                raise ValueError(_("The two passphrases differ"))
            bundle = crypto.make_recipient(passphrase)
        elif mode == "key":
            crypto.parse_recipient(recipient)
            bundle = recipient.strip()
    except (ValueError, crypto.CryptoError) as exc:
        flash(request, str(exc), "err")
        return back("/admin/backups")
    t = BackupTarget(owner_id=admin.id, owner_name=admin.username, mode=mode, recipient=bundle, **fields)
    db.add(t)
    db.flush()
    record(db, admin, "backup.create", "backup", t.id, name=t.name, folder=t.folder, mode=mode)
    db.commit()
    flash(request, _("Backup “%(name)s” created: it runs every day at %(hour)s:00", name=t.name, hour=f"{t.hour:02d}") +
          (_(". Write down the passphrase: the gateway does not keep it and without it the copies cannot be opened") if mode == "passphrase" else ""))
    return back("/admin/backups")


@router.get("/{tid}/edit", response_class=HTMLResponse)
def edit_form(request: Request, tid: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    t = _get(db, tid)
    if not _can_edit(t, admin):
        raise HTTPException(403, "this backup belongs to another administrator (or to .env)")
    return render(request, "backup_edit.html", admin, t=t, root=settings.backup_root, folders=_folders())


@router.post("/{tid}", dependencies=[Depends(verify_csrf)])
def update(request: Request, tid: int, name: str = Form(""), folder: str = Form(""), hour: str = Form("3"),
           keep_daily: str = Form("7"), keep_weekly: str = Form("4"), keep_monthly: str = Form("12"),
           require_marker: str = Form(""), admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    t = _get(db, tid)
    if not _can_edit(t, admin):
        raise HTTPException(403)
    try:
        fields = _common(name, folder, hour, keep_daily, keep_weekly, keep_monthly, bool(require_marker))
    except ValueError as exc:
        flash(request, str(exc), "err")
        return back(f"/admin/backups/{tid}/edit")
    for k, v in fields.items():
        setattr(t, k, v)
    record(db, admin, "backup.update", "backup", t.id, name=t.name, folder=t.folder)
    db.commit()
    flash(request, _("Backup “%(name)s” updated", name=t.name))
    return back("/admin/backups")


@router.post("/{tid}/toggle", dependencies=[Depends(verify_csrf)])
def toggle(request: Request, tid: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    t = _get(db, tid)
    if not _can_edit(t, admin):
        raise HTTPException(403)
    t.enabled = not t.enabled
    record(db, admin, "backup.enable" if t.enabled else "backup.disable", "backup", t.id, name=t.name)
    db.commit()
    flash(request, _("Backup “%(name)s” activated", name=t.name) if t.enabled else _("Backup “%(name)s” suspended", name=t.name))
    return back("/admin/backups")


@router.post("/{tid}/run", dependencies=[Depends(verify_csrf)])
async def run_now(request: Request, tid: int, admin: User = Depends(current_admin)):
    """Any administrator may start any backup (it is a safe, read-only operation on the data)."""
    sm = request.app.state.sessionmaker
    ok, message = await run_in_threadpool(lambda: service.run_target(sm, tid, prune=False))

    def note():
        with sm() as db:
            record(db, admin, "backup.run", "backup", tid, ok=ok)
            db.commit()

    await run_in_threadpool(note)
    flash(request, _("Backup done: %(message)s", message=message) if ok else _("Backup FAILED: %(message)s", message=message), "ok" if ok else "err")
    return back("/admin/backups")


@router.post("/{tid}/delete", dependencies=[Depends(verify_csrf)])
def delete(request: Request, tid: int, admin: User = Depends(current_admin), db: Session = Depends(get_db)):
    t = _get(db, tid)
    if not _can_edit(t, admin):
        raise HTTPException(403)
    record(db, admin, "backup.delete", "backup", t.id, name=t.name, folder=t.folder)
    db.delete(t)
    db.commit()
    flash(request, _("Backup “%(name)s” deleted. The files already written in the folder stay where they are", name=t.name))
    return back("/admin/backups")
