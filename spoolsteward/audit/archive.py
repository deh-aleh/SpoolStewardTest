"""Audit retention: kept forever, moved to compressed files when the active tables get heavy.

Nothing in the application deletes audit rows except this module, and only AFTER the same rows are safely in a
verified compressed file that is listed in `audit_archive`. The portal can read and download archives, never
remove them. Rows of the quota windows still in play are never archived, so limits are not affected.

Crash safety, in order:
  1. write <file>.pending (fsync) and re-read it to verify
  2. ONE transaction: register the archive + delete the archived rows
  3. rename <file>.pending -> <file>, make it read-only
A crash after 1 leaves a stray .pending file whose rows are still in the database (removed next run); a crash
after 2 leaves a registry entry whose file is still named .pending (renamed next run).
"""

import gzip
import hashlib
import json
import logging
import os
import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, delete, func, select
from starlette.concurrency import run_in_threadpool

from spoolsteward import crypto_fields, titles
from spoolsteward.timeutil import aware, local_tz
from spoolsteward.audit.adminlog import record
from spoolsteward.config import settings
from spoolsteward.db.models import AdminAudit, AuditArchive, JobLog, User

log = logging.getLogger(__name__)
TABLES = {"job_log": JobLog, "admin_audit": AdminAudit}


class ArchiveError(RuntimeError):
    pass


@dataclass
class ArchiveResult:
    table: str
    path: Path
    rows: int
    bytes: int
    sha256: str


def estimate_bytes(db, table: str) -> int:
    """Rough size of the active table's data: enough to decide when it is 'heavy'."""
    L = lambda col: func.length(func.coalesce(col, ""))  # noqa: E731
    if table == "job_log":
        expr = 64 + L(JobLog.title) + L(JobLog.printer) + L(JobLog.denied_reason) + L(cast(JobLog.options_json, String))
        return db.scalar(select(func.coalesce(func.sum(expr), 0))) or 0
    expr = 64 + L(AdminAudit.action) + L(AdminAudit.target_type) + L(AdminAudit.actor_name) \
        + L(cast(AdminAudit.detail_json, String))
    return db.scalar(select(func.coalesce(func.sum(expr), 0))) or 0


def protected_from(now: datetime, tz: ZoneInfo) -> datetime:
    """Rows from here on are never archived: the previous calendar month plus the current one cover every
    weekly/monthly quota window that can still matter."""
    local = now.astimezone(tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev = local.replace(year=local.year - (local.month == 1), month=(local.month - 2) % 12 + 1)
    return prev.astimezone(UTC)


def _jsonable(v):
    return aware(v).isoformat() if isinstance(v, datetime) else getattr(v, "value", v)


def _row_dict(table: str, obj, usernames: dict[int, str]) -> dict:
    if table == "job_log":
        title, options = obj.title, obj.options_json
        cut = titles.cutoff_for_archive()
        if cut is not None and aware(obj.ts) < cut:  # past the retention: the name does not go into the file
            title, options = None, {k: v for k, v in (options or {}).items() if k != "document_name"} or None
        else:  # the file keeps the names encrypted, like the database (the portal decrypts them when it shows them)
            title = crypto_fields.seal(title)
            if options and isinstance(options.get("document_name"), str):
                options = {**options, "document_name": crypto_fields.seal(options["document_name"])}
        return {
            "id": obj.id, "ts": _jsonable(obj.ts), "user_id": obj.user_id, "user": usernames.get(obj.user_id) or obj.user_name,
            "kind": _jsonable(obj.kind), "printer": obj.printer, "cups_job_id": obj.cups_job_id, "title": title,
            "pages": obj.pages, "state": _jsonable(obj.state), "reason": obj.denied_reason, "options": options,
        }
    return {
        "id": obj.id, "ts": _jsonable(obj.ts), "actor_id": obj.actor_id, "actor": obj.actor_name,
        "action": obj.action, "target_type": obj.target_type, "target_id": obj.target_id, "detail": obj.detail_json,
    }


def _final_path(directory: Path, table: str, first_id: int, last_id: int) -> Path:
    return directory / f"audit-{table}-{first_id:09d}-{last_id:09d}.jsonl.gz"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_pending(pending: Path, table: str, rows: list, usernames: dict[int, str]) -> tuple[int, str]:
    """Deterministic gzip (mtime 0): writing the same rows twice gives the same bytes."""
    with open(pending, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=6) as gz:
            for obj in rows:
                gz.write((json.dumps(_row_dict(table, obj, usernames), ensure_ascii=False, separators=(",", ":"))
                          + "\n").encode("utf-8"))
        raw.flush()
        os.fsync(raw.fileno())
    return pending.stat().st_size, _sha256(pending)


def count_lines(path: Path) -> int:
    with gzip.open(path, "rb") as f:
        return sum(1 for _ in f)


def repair(sm, directory: Path) -> None:
    """Finish or clean up after a crash (see the module docstring)."""
    if not directory.is_dir():
        return
    with sm() as db:
        registered = {Path(a.path) for a in db.scalars(select(AuditArchive))}
    for path in registered:
        pending = path.with_name(path.name + ".pending")
        if not path.exists() and pending.exists():
            os.replace(pending, path)
            os.chmod(path, 0o444)
            log.warning("audit archive: finished an interrupted rename of %s", path.name)
    for stray in directory.glob("audit-*.jsonl.gz.pending"):
        if Path(str(stray)[: -len(".pending")]) not in registered:
            stray.unlink()  # crash before the commit: its rows are still in the database
            log.warning("audit archive: removed unfinished file %s", stray.name)


def archive_table(sm, table: str, now: datetime, tz: ZoneInfo, directory: Path, max_bytes: int,
                  force: bool = False) -> ArchiveResult | None:
    model = TABLES[table]
    directory.mkdir(parents=True, exist_ok=True)
    repair(sm, directory)
    with sm() as db:
        if not force and estimate_bytes(db, table) <= max_bytes:
            return None
        cutoff = protected_from(now, tz)
        rows = list(db.scalars(select(model).where(model.ts < cutoff).order_by(model.id)))
        if not rows:
            log.info("audit archive: %s is over the threshold but everything is inside the protected window", table)
            return None
        usernames = {u.id: u.username for u in db.scalars(select(User))}
        first, last = rows[0], rows[-1]
        final = _final_path(directory, table, first.id, last.id)
        if final.exists() or db.scalars(select(AuditArchive).where(
                AuditArchive.source_table == table, AuditArchive.first_id == first.id,
                AuditArchive.last_id == last.id)).first():
            raise ArchiveError(f"{final.name} already exists: refusing to overwrite an archive")
        pending = final.with_name(final.name + ".pending")
        size, digest = _write_pending(pending, table, rows, usernames)
        if count_lines(pending) != len(rows):  # verify what is on disk, not what we think we wrote
            pending.unlink()
            raise ArchiveError("verification failed: line count differs, nothing was removed")
        ids = [r.id for r in rows]
        db.add(AuditArchive(source_table=table, path=str(final), first_ts=aware(first.ts), last_ts=aware(last.ts),
                            first_id=first.id, last_id=last.id, rows=len(rows), bytes=size, sha256=digest,
                            created_at=now))
        for i in range(0, len(ids), 500):
            db.execute(delete(model).where(model.id.in_(ids[i:i + 500])))
        record(db, None, "audit.archive", table, None, file=final.name, rows=len(rows), bytes=size, sha256=digest,
               first_id=first.id, last_id=last.id)
        db.commit()
    os.replace(pending, final)
    os.chmod(final, 0o444)
    log.info("audit archive: %d rows of %s -> %s (%d bytes)", len(rows), table, final, size)
    return ArchiveResult(table, final, len(rows), size, digest)


def archive_all(sm, now: datetime | None = None, force: bool = False) -> list[ArchiveResult]:
    now = now or datetime.now(UTC)
    tz = local_tz()
    out = []
    for table in TABLES:
        try:
            res = archive_table(sm, table, now, tz, Path(settings.audit_archive_dir),
                                0 if force else settings.audit_max_mb << 20, force)
        except (OSError, ArchiveError):
            log.exception("audit archive of %s failed (nothing was removed)", table)
            continue
        if res:
            out.append(res)
    return out


# ---------------------------------------------------------------- reading (portal)

def archive_status(a: AuditArchive, verify: bool = False) -> str:
    path = Path(a.path)
    if not path.exists():
        return "missing"
    if path.stat().st_size != a.bytes:
        return "modified"
    if verify and _sha256(path) != a.sha256:
        return "modified"
    return "ok" if verify else "present"


def _unsealed(row: dict) -> dict:
    """A line of an archive file with the encrypted file names opened (the file itself keeps them encrypted)."""
    if crypto_fields.is_sealed(row.get("title")):
        row["title"] = crypto_fields.unseal(row["title"])
    opts = row.get("options")
    if isinstance(opts, dict) and crypto_fields.is_sealed(opts.get("document_name")):
        opts["document_name"] = crypto_fields.unseal(opts["document_name"])
    return row


def iter_archive(path: Path) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield _unsealed(json.loads(line))


def read_page(path: Path, page: int, per_page: int, query: str = "") -> tuple[list[dict], int]:
    """(rows of one page, total matching rows), streaming the file."""
    q = query.strip().lower()
    start, end, total, out = (page - 1) * per_page, page * per_page, 0, []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            row = None
            if crypto_fields.PREFIX in line:  # search in the names as people read them, not as they are stored
                row = _unsealed(json.loads(line))
                line = json.dumps(row, ensure_ascii=False)
            if q and q not in line.lower():
                continue
            if start <= total < end:
                out.append(row or json.loads(line))
            total += 1
    return out, total


async def run_archiver(app) -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await run_in_threadpool(archive_all, app.state.sessionmaker)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("audit archiver failed")
        await asyncio.sleep(settings.audit_check_interval)
