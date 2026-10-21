"""Scheduling and bookkeeping of backups: which targets exist, which one is due, running one, recording the outcome."""

import asyncio
import logging
import secrets
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from spoolsteward import notices
from spoolsteward.timeutil import aware, local_tz
from spoolsteward.backup import core
from spoolsteward.config import settings
from spoolsteward.db.models import BackupTarget, GatewaySetting
from spoolsteward.i18n import _

log = logging.getLogger("spoolsteward.backup")
RETRY_AFTER = timedelta(hours=1)
STALE_AFTER = timedelta(hours=48)
GLOBAL_NAME = "Generale (.env)"
_RUN = threading.Lock()  # one backup at a time: they all read the same database


def instance_id(db) -> str:
    """Random, stored once in the database: it travels with a restore and keeps two gateways that share a
    destination from pruning each other's files."""
    row = db.get(GatewaySetting, "instance_id")
    if row is None:
        row = GatewaySetting(key="instance_id", value=secrets.token_hex(4))
        db.add(row)
        db.commit()
    return row.value


def sync_global(db) -> None:
    """The general backup lives in .env; keep its row in step so the portal and the scheduler treat it alike."""
    row = db.scalars(select(BackupTarget).where(BackupTarget.is_global)).first()
    if not settings.backup_enabled and row is None:
        return
    if row is None:
        row = BackupTarget(is_global=True, owner_name=".env", name=GLOBAL_NAME, folder=settings.backup_dir)
        db.add(row)
    want = dict(
        enabled=settings.backup_enabled, folder=settings.backup_dir, require_marker=settings.backup_require_marker, hour=min(max(settings.backup_hour, 0), 23),
        keep_daily=max(settings.backup_keep_daily, 1), keep_weekly=max(settings.backup_keep_weekly, 0),
        keep_monthly=max(settings.backup_keep_monthly, 0),
        mode="key" if settings.backup_recipient.strip() else "none", recipient=settings.backup_recipient.strip() or None,
    )
    if any(getattr(row, k) != v for k, v in want.items()):
        for k, v in want.items():
            setattr(row, k, v)
    db.commit()


def is_due(t: BackupTarget, now: datetime) -> bool:
    if not t.enabled:
        return False
    local = now.astimezone(local_tz())
    ok = aware(t.last_ok_at)
    if ok is not None and ok.astimezone(local_tz()).date() >= local.date():
        return False  # already done today
    if local.hour < t.hour:
        return False
    tried = aware(t.last_attempt_at)
    return not (t.last_status == "error" and tried is not None and now - tried < RETRY_AFTER)


def health(t: BackupTarget, now: datetime) -> str:
    """ok | stale (enabled but no success for 48 h) | error | off | never"""
    if not t.enabled:
        return "off"
    ok = aware(t.last_ok_at)
    if t.last_status == "error" and (ok is None or now - ok > timedelta(hours=1)):
        return "error"
    if ok is None:
        return "never" if now - (aware(t.created_at) or now) < STALE_AFTER else "stale"
    return "stale" if now - ok > STALE_AFTER else "ok"


def run_target(sm, target_id: int, *, prune: bool = True) -> tuple[bool, str]:
    """Run one backup now (blocking). Returns (success, message) and records the outcome on the target.
    `prune=False` (the "run now" button) only adds a copy: old ones are removed by the next scheduled run."""
    if not _RUN.acquire(blocking=False):
        return False, _("Another backup is running: try again in a minute")
    try:
        with sm() as db:
            t = db.get(BackupTarget, target_id)
            if t is None:
                return False, _("Backup target not found")
            instance, now = instance_id(db), datetime.now(UTC)
            t.last_attempt_at = now
            db.commit()
            try:
                # with the marker required the folder is never created: an unmounted share must not get an empty local copy
                def preflight():
                    path = core.resolve_folder(t.folder, create=not t.require_marker)
                    if t.require_marker:
                        core.check_marker(path)
                    return path

                folder = core.bounded(preflight, timeout=20)  # a dead network disk fails here, before any big write
                res = core.create_backup(
                    sm, folder, core.tag(instance, t.id), recipient=t.recipient if t.mode != "none" else None,
                    target_name=t.name, instance=instance, now=now)
                res.mirrored = core.mirror_audit(Path(settings.audit_archive_dir), folder, instance,
                                                 t.recipient if t.mode != "none" else None)
                res.removed = core.prune(folder, instance, t.id, t.keep_daily, t.keep_weekly, t.keep_monthly) if prune else []
            except Exception as exc:
                message = str(exc) if isinstance(exc, (core.BackupError, OSError)) else f"{type(exc).__name__}: {exc}"
                log.error("backup %s (%s) failed: %s", t.id, t.name, message)
                t.last_status, t.last_error = "error", message[:1000]
                last_ok = t.last_ok_at
                db.commit()
                notices.report(sm, "backup_not_mounted" if isinstance(exc, core.NotMounted) else "backup_failed", str(t.id),
                               {"name": t.name, "error": message[:300], "last_ok": last_ok.isoformat() if last_ok else None})
                return False, message
            t.last_status, t.last_error, t.last_ok_at = "ok", None, now
            t.last_file, t.last_size = res.path.name, res.size
            db.commit()
            for kind in ("backup_not_mounted", "backup_failed"):
                notices.resolve(sm, kind, str(t.id))
            log.info("backup %s (%s): %s, %d KB, %d audit file(s) copied, %d old removed", t.id, t.name, res.path.name,
                     res.size // 1024, res.mirrored, len(res.removed))
            return True, f"{res.path.name} ({res.size // 1024} KB)"
    finally:
        _RUN.release()


def due_ids(sm) -> list[int]:
    with sm() as db:
        sync_global(db)
        now = datetime.now(UTC)
        return [t.id for t in db.scalars(select(BackupTarget)) if is_due(t, now)]


async def run_scheduler(app) -> None:
    await asyncio.sleep(45)
    while True:
        try:
            for tid in await run_in_threadpool(due_ids, app.state.sessionmaker):
                await run_in_threadpool(run_target, app.state.sessionmaker, tid)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("backup scheduler failed")
        await asyncio.sleep(60)
