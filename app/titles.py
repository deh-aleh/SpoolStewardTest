"""Housekeeping of the file names kept in the audit: seal old plain ones, forget the ones past the retention."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, text
from sqlalchemy.orm.attributes import flag_modified
from starlette.concurrency import run_in_threadpool

from app import crypto_fields
from app.config import settings
from app.db.models import JobLog

log = logging.getLogger("app.titles")
BATCH = 500


def encrypt_existing(sm) -> int:
    """Seal the names written before the key existed (plain text in the database). Safe to repeat and to interrupt."""
    if not crypto_fields.enabled():
        return 0
    done = 0
    while True:
        with sm() as db:
            ids = [r[0] for r in db.execute(text(
                "SELECT id FROM job_log WHERE (title IS NOT NULL AND title NOT LIKE 'enc1:%') "
                "OR (json_extract(options_json, '$.document_name') IS NOT NULL "
                "AND json_extract(options_json, '$.document_name') NOT LIKE 'enc1:%') ORDER BY id LIMIT :n"), {"n": BATCH})]
            if not ids:
                break
            for row in db.scalars(select(JobLog).where(JobLog.id.in_(ids))):
                # the column types encrypt on write; flagging the values is what makes SQLAlchemy write them again
                flag_modified(row, "title")
                if row.options_json:
                    flag_modified(row, "options_json")
            db.commit()
            done += len(ids)
        if len(ids) < BATCH:
            break
    if done:
        log.info("file names: %d old audit row(s) are now encrypted", done)
    return done


def purge(sm, days: int, now: datetime | None = None) -> int:
    """Erase the file names of the rows older than `days` days. The rows themselves stay."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    done = 0
    while True:
        with sm() as db:
            rows = db.scalars(select(JobLog).where(
                JobLog.ts < cutoff,
                or_(JobLog.title.isnot(None), text("json_extract(options_json, '$.document_name') IS NOT NULL")),
            ).order_by(JobLog.id).limit(BATCH)).all()
            if not rows:
                break
            for row in rows:
                row.title = None
                if row.options_json and "document_name" in row.options_json:
                    row.options_json = {k: v for k, v in row.options_json.items() if k != "document_name"}
            db.commit()
            done += len(rows)
    if done:
        log.info("file names: erased from %d audit row(s) older than %d days", done, days)
    return done


def cutoff_for_archive(now: datetime | None = None) -> datetime | None:
    """Rows older than this lose their names when they are written to an archive file (None: retention is off)."""
    if not settings.job_title_retention_enabled:
        return None
    return (now or datetime.now(UTC)) - timedelta(days=max(settings.job_title_retention_days, 0))


async def run_titles(app) -> None:
    """At start seal the old names, then once a day apply the retention."""
    await asyncio.sleep(20)
    try:
        await run_in_threadpool(encrypt_existing, app.state.sessionmaker)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("could not encrypt the old file names (they stay readable as they are)")
    while True:
        if settings.job_title_retention_enabled:
            try:
                await run_in_threadpool(purge, app.state.sessionmaker, max(settings.job_title_retention_days, 0))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("file name retention failed")
        await asyncio.sleep(24 * 3600)
