"""Phase 3: the audit is kept forever; heavy tables are moved to verified compressed files, never deleted."""

import gzip
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from spoolsteward.audit import archive
from spoolsteward.audit.adminlog import record
from spoolsteward.config import settings
from spoolsteward.db.models import AdminAudit, AuditArchive, JobKind, JobLog, JobState, Metric, PolicyRule, SubjectType, User
from spoolsteward.policy.engine import usage
from tests.conftest import csrf_of_page, make_user, portal_login

GOOD = "correct horse battery"
TZ = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
OLD = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)      # long before the protected window (starts 2026-09-01)
RECENT = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)   # inside the quota windows in play


def add_jobs(sm, user_id, n, ts, title="file", state=JobState.completed, pages=1, start=0):
    with sm() as s:
        s.add_all([JobLog(ts=ts + timedelta(minutes=i), user_id=user_id, kind=JobKind.print, printer="p",
                          title=f"{title}-{start + i}.pdf", pages=pages, state=state, cups_job_id=start + i + 1)
                   for i in range(n)])
        s.commit()


def count(sm, model):
    with sm() as s:
        return s.scalar(select(func.count(model.id)))


@pytest.fixture
def world(portal, tmp_path):
    uid = make_user(portal.sm, "ann", password=GOOD, group="staff")
    portal.uid, portal.dir = uid, Path(settings.audit_archive_dir)
    return portal


def run(world, force=True, now=NOW):
    return archive.archive_all(world.sm, now=now, force=force)


# ---------------------------------------------------------------- the decision to archive

def test_nothing_happens_below_the_threshold(world):
    add_jobs(world.sm, world.uid, 20, OLD)
    assert run(world, force=False) == []                          # 50 MB default threshold
    assert count(world.sm, JobLog) == 20 and count(world.sm, AuditArchive) == 0


def test_the_threshold_is_the_weight_of_the_active_table(world):
    add_jobs(world.sm, world.uid, 50, OLD, title="x" * 400)
    with world.sm() as s:
        size = archive.estimate_bytes(s, "job_log")
    assert size > 50 * 400                                        # it measures the text, not the row count
    assert archive.archive_table(world.sm, "job_log", NOW, TZ, world.dir, size + 1) is None   # not over yet
    assert archive.archive_table(world.sm, "job_log", NOW, TZ, world.dir, size - 1) is not None


def test_rows_inside_the_quota_windows_in_play_are_never_archived(world):
    add_jobs(world.sm, world.uid, 10, RECENT)
    assert run(world) == [] and count(world.sm, JobLog) == 10


# ---------------------------------------------------------------- what archiving does

def test_old_rows_go_to_a_verified_file_and_quotas_do_not_change(world):
    with world.sm() as s:
        s.add(PolicyRule(subject_type=SubjectType.user, subject_id=world.uid, metric=Metric.jobs, limit=100))
        s.add(PolicyRule(subject_type=SubjectType.user, subject_id=world.uid, metric=Metric.pages, limit=100))
        s.commit()
    add_jobs(world.sm, world.uid, 300, OLD)
    add_jobs(world.sm, world.uid, 7, RECENT, title="recent", pages=2)
    with world.sm() as s:
        user = s.get(User, world.uid)
        rules = s.scalars(select(PolicyRule)).all()
        before = [usage(s, user, r, NOW, TZ) for r in rules]
    [res] = [r for r in run(world) if r.table == "job_log"]
    assert res.rows == 300 and res.path.parent == world.dir
    # the file: read-only, valid gzip, one JSON object per row, sha256 recorded in the registry
    assert oct(res.path.stat().st_mode & 0o777) == "0o444"
    lines = gzip.open(res.path, "rt").read().splitlines()
    assert len(lines) == 300 and json.loads(lines[0])["user"] == "ann" and json.loads(lines[0])["title"] == "file-0.pdf"
    assert hashlib.sha256(res.path.read_bytes()).hexdigest() == res.sha256
    with world.sm() as s:
        a = s.scalars(select(AuditArchive).where(AuditArchive.source_table == "job_log")).one()
        assert (a.rows, a.sha256, a.bytes, Path(a.path)) == (300, res.sha256, res.bytes, res.path)
        assert (a.first_ts.replace(tzinfo=UTC), a.first_id) == (OLD, 1)
        # the active table keeps exactly the protected rows, and nobody's quota moved
        assert s.scalar(select(func.count(JobLog.id))) == 7
        assert [usage(s, s.get(User, world.uid), r, NOW, TZ) for r in s.scalars(select(PolicyRule))] == before
        entry = s.scalars(select(AdminAudit).where(AdminAudit.action == "audit.archive")).first()
        assert entry.actor_name == "cli" and entry.detail_json["file"] == res.path.name


def test_a_second_run_archives_only_what_became_old_since(world):
    add_jobs(world.sm, world.uid, 5, OLD)
    run(world)
    add_jobs(world.sm, world.uid, 5, OLD + timedelta(days=1), start=100)
    run(world)
    files = sorted(p.name for p in world.dir.glob("audit-job_log-*.jsonl.gz"))
    assert len(files) == 2                                          # ids kept growing although the table was emptied
    with world.sm() as s:
        archives = s.scalars(select(AuditArchive).where(AuditArchive.source_table == "job_log")).all()
        assert [a.rows for a in archives] == [5, 5] and archives[0].last_id < archives[1].first_id
        assert s.scalar(select(func.count(JobLog.id))) == 0


def test_admin_actions_are_archived_the_same_way(world):
    with world.sm() as s:
        for i in range(12):
            row = record(s, None, "test.action", "user", i, n=i)
            row.ts = OLD + timedelta(minutes=i)
        s.commit()
    res = {r.table: r for r in run(world)}
    assert res["admin_audit"].rows == 12
    rows = [json.loads(l) for l in gzip.open(res["admin_audit"].path, "rt")]
    assert rows[3]["action"] == "test.action" and rows[3]["detail"] == {"n": 3} and rows[3]["actor"] == "cli"
    with world.sm() as s:   # the only admin_audit row left is the one that says it was archived
        assert [a.action for a in s.scalars(select(AdminAudit))] == ["audit.archive"]


# ---------------------------------------------------------------- crashes and refusals

def test_a_crash_after_writing_but_before_the_commit_loses_nothing(world, monkeypatch):
    add_jobs(world.sm, world.uid, 10, OLD)
    real_count = archive.count_lines
    monkeypatch.setattr(archive, "count_lines", lambda p: (_ for _ in ()).throw(RuntimeError("power cut")))
    with pytest.raises(RuntimeError):
        archive.archive_table(world.sm, "job_log", NOW, TZ, world.dir, 0, True)
    assert count(world.sm, JobLog) == 10 and count(world.sm, AuditArchive) == 0      # nothing removed
    assert list(world.dir.glob("*.pending"))                                          # a stray file is left behind
    monkeypatch.setattr(archive, "count_lines", real_count)
    [res] = [r for r in run(world) if r.table == "job_log"]
    assert not list(world.dir.glob("*.pending"))                                      # cleaned up by the next run
    assert count(world.sm, JobLog) == 0 and res.rows == 10 and len(list(world.dir.glob("audit-job_log-*"))) == 1


def test_a_crash_after_the_commit_but_before_the_rename_is_finished_later(world):
    add_jobs(world.sm, world.uid, 10, OLD)
    [res] = [r for r in run(world) if r.table == "job_log"]
    os.chmod(res.path, 0o644)
    res.path.rename(res.path.with_name(res.path.name + ".pending"))                   # what a crash there leaves
    with world.sm() as s:
        assert archive.archive_status(s.scalars(select(AuditArchive)).first()) == "missing"
    archive.repair(world.sm, world.dir)
    with world.sm() as s:
        a = s.scalars(select(AuditArchive).where(AuditArchive.source_table == "job_log")).one()
        assert archive.archive_status(a, verify=True) == "ok"
    assert oct(res.path.stat().st_mode & 0o777) == "0o444"


def test_an_existing_archive_is_never_overwritten(world):
    add_jobs(world.sm, world.uid, 5, OLD)
    world.dir.mkdir(parents=True, exist_ok=True)
    blocker = world.dir / "audit-job_log-000000001-000000005.jsonl.gz"
    blocker.write_bytes(b"precious")
    assert [r for r in run(world) if r.table == "job_log"] == []
    assert blocker.read_bytes() == b"precious" and count(world.sm, JobLog) == 5


def test_an_unwritable_folder_means_nothing_is_removed(world):
    add_jobs(world.sm, world.uid, 5, OLD)
    world.dir.parent.mkdir(parents=True, exist_ok=True)
    world.dir.write_text("a file where the folder should be")
    assert run(world) == [] and count(world.sm, JobLog) == 5


def test_status_notices_a_missing_or_tampered_file(world):
    add_jobs(world.sm, world.uid, 5, OLD)
    [res] = [r for r in run(world) if r.table == "job_log"]
    with world.sm() as s:
        a = s.scalars(select(AuditArchive).where(AuditArchive.source_table == "job_log")).one()
        assert archive.archive_status(a) == "present" and archive.archive_status(a, verify=True) == "ok"
        os.chmod(res.path, 0o644)
        same_size = bytearray(res.path.read_bytes())
        same_size[-5] ^= 0xFF                                                             # flip a byte, keep the size
        res.path.write_bytes(bytes(same_size))
        assert archive.archive_status(a) == "present"                                     # the cheap check can't see it
        assert archive.archive_status(a, verify=True) == "modified"                       # the sha256 can
        res.path.write_bytes(b"short")
        assert archive.archive_status(a) == "modified"
        res.path.unlink()
        assert archive.archive_status(a) == "missing"


def test_cli_archives_on_demand(world, monkeypatch, capsys):
    from spoolsteward import cli

    add_jobs(world.sm, world.uid, 4, OLD)
    monkeypatch.setattr(cli, "SessionLocal", world.sm)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    assert cli.main(["audit-archive"]) == 0 and "nothing to archive" in capsys.readouterr().out
    assert cli.main(["audit-archive", "--force"]) == 0 and "job_log: 4 rows" in capsys.readouterr().out


# ---------------------------------------------------------------- the portal

@pytest.fixture
async def admin(world):          # own fixture on purpose: built on `world` (archive data, own db), not on the shared `portal`
    make_user(world.sm, "boss", password=GOOD, admin=True)
    add_jobs(world.sm, world.uid, 250, OLD, title="report")
    add_jobs(world.sm, world.uid, 3, RECENT, title="recent")
    [res] = [r for r in run(world) if r.table == "job_log"]
    world.res = res
    await portal_login(world, "boss", GOOD)
    world.token = await csrf_of_page(world, "/admin/users")
    with world.sm() as s:
        world.aid = s.scalars(select(AuditArchive).where(AuditArchive.source_table == "job_log")).one().id
    return world


async def test_the_audit_page_warns_that_older_rows_are_archived(admin):
    html = (await admin.get("/admin/jobs")).text
    assert "There is 1 compressed audit archive" in html and "/admin/archive" in html
    assert "recent-0.pdf" in html and "report-0.pdf" not in html                  # the old rows left the table


async def test_the_archive_page_shows_both_paths_and_the_checksum(admin, monkeypatch):
    monkeypatch.setattr(settings, "audit_archive_host_path", "/home/me/project/data/audit-archive")
    html = (await admin.get("/admin/archive")).text
    name = admin.res.path.name
    assert name in html and str(admin.res.path) in html                               # path inside the container
    assert f"/home/me/project/data/audit-archive/{name}" in html                      # path on the server
    assert admin.res.sha256 in html and "250" in html


async def test_an_archive_can_be_read_searched_and_paged_in_the_portal(admin):
    page1 = (await admin.get(f"/admin/archive/{admin.aid}/view")).text
    assert "report-0.pdf" in page1 and "report-99.pdf" in page1 and "report-100.pdf" not in page1
    assert "Page 1 / 3" in page1
    assert "report-249.pdf" in (await admin.get(f"/admin/archive/{admin.aid}/view?page=3")).text
    hit = (await admin.get(f"/admin/archive/{admin.aid}/view?q=report-17.pdf")).text
    assert "report-17.pdf" in hit and "1 results" in hit and "report-18.pdf" not in hit


async def test_the_archive_can_be_downloaded_byte_for_byte(admin):
    r = await admin.get(f"/admin/archive/{admin.aid}/download")
    assert r.status_code == 200 and r.content == admin.res.path.read_bytes()
    assert r.headers["content-disposition"].endswith(f'{admin.res.path.name}"')


async def test_verify_reports_integrity_and_tampering(admin):
    await admin.post(f"/admin/archive/{admin.aid}/verify", data={"csrf_token": admin.token})
    assert "integrity OK" in (await admin.get("/admin/archive")).text
    os.chmod(admin.res.path, 0o644)
    data = bytearray(admin.res.path.read_bytes())
    data[-5] ^= 0xFF
    admin.res.path.write_bytes(bytes(data))
    await admin.post(f"/admin/archive/{admin.aid}/verify", data={"csrf_token": admin.token})
    assert "MODIFIED" in (await admin.get("/admin/archive")).text


async def test_nothing_in_the_portal_can_delete_audit_data(admin):
    from spoolsteward.main import app

    for route in app.routes:
        path, methods = getattr(route, "path", ""), getattr(route, "methods", set()) or set()
        assert "DELETE" not in methods and "PUT" not in methods and "PATCH" not in methods, path
        if path.startswith(("/admin/archive", "/admin/jobs", "/admin/actions")):
            assert "delete" not in path.lower() and "remove" not in path.lower(), path
    r = await admin.post(f"/admin/archive/{admin.aid}/download", data={"csrf_token": admin.token})
    assert r.status_code in (404, 405, 415) and admin.res.path.exists()               # no POST route reaches the file
    assert (await admin.get("/admin/archive/9999/download")).status_code == 404       # ids come from the registry, not paths
    assert admin.res.path.exists()


async def test_archives_are_for_administrators_only(admin):
    await admin.post("/logout", data={"csrf_token": admin.token})
    await portal_login(admin, "ann", GOOD)
    for path in ("/admin/archive", f"/admin/archive/{admin.aid}/view", f"/admin/archive/{admin.aid}/download"):
        assert (await admin.get(path)).status_code == 403
