"""Backups: encryption with a recovery key, consistent snapshots, retention, safe folders, per-administrator targets."""

import io
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from spoolsteward.backup import core, crypto, service
from spoolsteward.config import settings
from spoolsteward.db.models import AdminAudit, BackupTarget
from tests.conftest import csrf_of_page, make_user, portal_login

GOOD = "correct horse battery"
PASS = "a long backup passphrase"


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    root, cfg, audit = tmp_path / "backups", tmp_path / "cfg", tmp_path / "archive"
    for d in (root, cfg / "tls", audit):
        d.mkdir(parents=True)
    (cfg / "gateway.env").write_text("SPOOLSTEWARD_SECRET_KEY=very-secret\n")
    (cfg / "tls" / "privkey.pem").write_text("PRIVATE KEY")
    (audit / "job_log-1.jsonl.gz").write_bytes(b"audit-bytes" * 100)
    monkeypatch.setattr(settings, "backup_root", str(root))
    monkeypatch.setattr(settings, "backup_config_dir", str(cfg))
    monkeypatch.setattr(settings, "audit_archive_dir", str(audit))
    return root


# ------------------------------------------------------------------------------------------------ crypto

def test_encryption_roundtrip_and_tampering():
    bundle = crypto.make_recipient(PASS)
    data = os.urandom(2_500_000)
    out = io.BytesIO()
    w = crypto.EncryptWriter(out, bundle)
    w.write(data[:7])
    w.write(data[7:])
    w.close()
    blob = out.getvalue()
    assert data[:50] not in blob
    assert crypto.DecryptReader(io.BytesIO(blob), PASS).read() == data
    with pytest.raises(crypto.CryptoError, match="wrong passphrase"):
        crypto.DecryptReader(io.BytesIO(blob), "another passphrase!!")
    with pytest.raises(crypto.CryptoError):
        crypto.DecryptReader(io.BytesIO(blob[:-3]), PASS).read()                  # truncated
    flipped = bytearray(blob)
    flipped[len(blob) // 2] ^= 1
    with pytest.raises(crypto.CryptoError):
        crypto.DecryptReader(io.BytesIO(bytes(flipped)), PASS).read()             # modified


def test_passphrase_rules_and_bad_recipient():
    with pytest.raises(crypto.CryptoError):
        crypto.make_recipient("short")
    with pytest.raises(crypto.CryptoError):
        crypto.parse_recipient("cpgbk1.AAAA.BBBB")
    with pytest.raises(crypto.CryptoError):
        crypto.parse_recipient("something else")


# ------------------------------------------------------------------------------------------------ folders

def test_folder_cannot_leave_the_backup_root(dirs, tmp_path):
    (tmp_path / "outside").mkdir()
    (dirs / "link").symlink_to(tmp_path / "outside")
    for bad in ("../outside", "/etc", "a/../../x", "link", "link/sub", ".hidden", "a b\x00c", "x" * 100, ""):
        with pytest.raises(core.BackupError):
            core.resolve_folder(bad)
    assert core.resolve_folder("nas/office") == (dirs / "nas" / "office").resolve()
    assert (dirs / "nas" / "office").is_dir()


def test_no_root_means_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_root", str(tmp_path / "missing"))
    with pytest.raises(core.BackupError, match="not available"):
        core.resolve_folder("x")


# ------------------------------------------------------------------------------------------------ retention

def test_retention_keeps_newest_per_period():
    now = datetime(2026, 10, 11, 12, tzinfo=UTC)
    stamps = [now - timedelta(days=d) for d in range(0, 90)]          # one per day, newest first
    keep = core.keep_set(stamps, 7, 4, 3, "UTC")
    assert {0, 1, 2, 3, 4, 5, 6} <= keep                                  # the last 7 days
    assert len(keep) <= 7 + 4 + 3 and len(keep) >= 7 + 3
    assert core.keep_set([], 7, 4, 12, "UTC") == set()
    assert core.keep_set(stamps[:1], 1, 0, 0, "UTC") == {0}


def test_prune_only_touches_its_own_files(dirs):
    f = core.resolve_folder("p")
    mine = [f / f"spoolsteward-aaaaaaaa-t1-2026010{d}-030000.tar.gz" for d in range(1, 6)]
    others = [f / "spoolsteward-bbbbbbbb-t1-20260101-030000.tar.gz", f / "spoolsteward-aaaaaaaa-t2-20260101-030000.tar.gz",
              f / "notes.txt"]
    for p in mine + others:
        p.write_bytes(b"x")
    removed = core.prune(f, "aaaaaaaa", 1, 2, 0, 0)
    assert len(removed) == 3 and all(p.exists() for p in others)
    assert {p.name for p in mine if p.exists()} == {mine[3].name, mine[4].name}


# ------------------------------------------------------------------------------------------------ backups

async def test_plain_backup_has_no_secrets_and_encrypted_has_them(portal, dirs):
    make_user(portal.sm, "ada", password=GOOD, admin=True)
    f = core.resolve_folder("t")
    plain = core.create_backup(portal.sm, f, "spoolsteward-aaaaaaaa-t1", recipient=None, target_name="p", instance="aaaaaaaa")
    m = core.verify(plain.path)
    assert set(m["files"]) == {"spoolsteward.db"} and not m["secrets_included"]

    bundle = crypto.make_recipient(PASS)
    enc = core.create_backup(portal.sm, f, "spoolsteward-aaaaaaaa-t2", recipient=bundle, target_name="e", instance="aaaaaaaa",
                             now=datetime.now(UTC) + timedelta(seconds=1))
    assert enc.path.name.endswith(".tar.gz.enc") and b"very-secret" not in enc.path.read_bytes()
    m = core.verify(enc.path, PASS)
    assert {"spoolsteward.db", "config/gateway.env", "config/tls/privkey.pem"} == set(m["files"])
    with pytest.raises(crypto.CryptoError):
        core.verify(enc.path, "not the passphrase!!")


async def test_the_env_file_travels_in_encrypted_backups_only(portal, dirs, tmp_path, monkeypatch):
    env = tmp_path / "project.env"
    env.write_text("SPOOLSTEWARD_SECRET_KEY=from-env\n")
    monkeypatch.setattr(settings, "backup_env_file", str(env))
    (dirs.parent / "cfg" / ".cert-sync.abc123").mkdir()
    (dirs.parent / "cfg" / ".cert-sync.abc123" / "privkey.pem").write_text("TEMPORARY")
    f = core.resolve_folder("t")
    plain = core.create_backup(portal.sm, f, "spoolsteward-aaaaaaaa-t1", recipient=None, target_name="p", instance="aaaaaaaa")
    assert set(core.verify(plain.path)["files"]) == {"spoolsteward.db"}
    enc = core.create_backup(portal.sm, f, "spoolsteward-aaaaaaaa-t2", recipient=crypto.make_recipient(PASS), target_name="e",
                             instance="aaaaaaaa", now=datetime.now(UTC) + timedelta(seconds=1))
    files = set(core.verify(enc.path, PASS)["files"])
    assert "config/.env" in files and not any(".cert-sync" in x for x in files)   # the script's temporary folder is skipped


async def test_a_damaged_backup_is_detected(portal, dirs):
    f = core.resolve_folder("t")
    res = core.create_backup(portal.sm, f, "spoolsteward-aaaaaaaa-t1", recipient=None, target_name="p", instance="aaaaaaaa")
    blob = bytearray(res.path.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    res.path.write_bytes(bytes(blob))
    with pytest.raises(core.BackupError):
        core.verify(res.path)


async def test_restore_brings_back_database_audit_and_config(portal, dirs, tmp_path):
    make_user(portal.sm, "ada", password=GOOD, admin=True)
    make_user(portal.sm, "bob", password=GOOD)
    f = core.resolve_folder("t")
    bundle = crypto.make_recipient(PASS)
    res = core.create_backup(portal.sm, f, "spoolsteward-aaaaaaaa-t1", recipient=bundle, target_name="e", instance="aaaaaaaa")
    assert core.mirror_audit(Path(settings.audit_archive_dir), f, "aaaaaaaa", bundle) == 1
    assert core.mirror_audit(Path(settings.audit_archive_dir), f, "aaaaaaaa", bundle) == 0   # already there
    assert b"audit-bytes" not in next((f / "audit-archive-aaaaaaaa").iterdir()).read_bytes()

    new = tmp_path / "new"
    new.mkdir()
    (new / "spoolsteward.db").write_bytes(b"old database")
    out = core.restore(res.path, PASS, db_path=new / "spoolsteward.db", config_out=new / "cfg", audit_dir=new / "audit")
    assert out["audit_files"] == 1 and out["config"]
    assert (new / "audit" / "job_log-1.jsonl.gz").read_bytes() == b"audit-bytes" * 100
    assert (new / "cfg" / "gateway.env").read_text().startswith("SPOOLSTEWARD_SECRET_KEY")
    assert list(new.glob("spoolsteward.db.before-restore-*"))[0].read_bytes() == b"old database"
    names = {r[0] for r in sqlite3.connect(new / "spoolsteward.db").execute("select username from user")}
    assert {"ada", "bob"} <= names


async def test_a_restore_that_cannot_work_leaves_the_current_database_alone(portal, dirs, tmp_path):
    import json, tarfile
    f = core.resolve_folder("t")
    new = tmp_path / "new"
    new.mkdir()
    (new / "spoolsteward.db").write_bytes(b"current database")

    def attempt(path, passphrase=None):
        with pytest.raises(core.BackupError):
            core.restore(path, passphrase, db_path=new / "spoolsteward.db", config_out=new / "cfg", audit_dir=new / "audit")
        assert (new / "spoolsteward.db").read_bytes() == b"current database" and not list(new.glob("*before-restore*"))

    empty = f / "spoolsteward-aaaaaaaa-t1-20260101-000000.tar.gz"   # a valid archive whose manifest lists no database
    with tarfile.open(empty, "w:gz") as tf:
        data = json.dumps({"format": 1, "schema": None, "instance": "aaaaaaaa", "files": {}}).encode()
        info = tarfile.TarInfo(core.MANIFEST)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    attempt(empty)

    good = core.create_backup(portal.sm, f, "spoolsteward-aaaaaaaa-t1", recipient=None, target_name="p", instance="aaaaaaaa")
    cut = f / "spoolsteward-aaaaaaaa-t1-20260102-000000.tar.gz"      # cut in half: an interrupted copy
    cut.write_bytes(good.path.read_bytes()[: good.size // 2])
    attempt(cut)

    bundle = crypto.make_recipient(PASS)
    enc = core.create_backup(portal.sm, f, "spoolsteward-aaaaaaaa-t1", recipient=bundle, target_name="e", instance="aaaaaaaa")
    with pytest.raises(Exception):                                # wrong passphrase
        core.restore(enc.path, "another long passphrase", db_path=new / "spoolsteward.db", config_out=new / "cfg", audit_dir=new / "audit")
    assert (new / "spoolsteward.db").read_bytes() == b"current database"


# ------------------------------------------------------------------------------------------------ portal

async def test_admins_have_their_own_backups(portal, dirs):
    make_user(portal.sm, "ada", password=GOOD, admin=True)
    make_user(portal.sm, "eli", password=GOOD, admin=True)
    make_user(portal.sm, "pia", password=GOOD)
    await portal_login(portal, "ada", GOOD)
    csrf = await csrf_of_page(portal, "/admin/backups")
    page = await portal.get("/admin/backups")
    assert "No backup configured" in page.text and 'value="passphrase" checked' in page.text

    bad = await portal.post("/admin/backups", data=dict(csrf_token=csrf, name="x", folder="../etc", mode="none"))
    assert bad.status_code == 303
    assert "Folder name not valid" in (await portal.get("/admin/backups")).text
    await portal.post("/admin/backups", data=dict(csrf_token=csrf, name="x", folder="ada", mode="passphrase",
                                                            passphrase=PASS, passphrase2="different one!!"))
    assert "passphrases differ" in (await portal.get("/admin/backups")).text
    with portal.sm() as s:
        assert s.scalars(select(BackupTarget)).first() is None

    ok = await portal.post("/admin/backups", data=dict(csrf_token=csrf, name="NAS ada", folder="nas/ada", hour="2",
                                                       keep_daily="3", keep_weekly="1", keep_monthly="1",
                                                       mode="passphrase", passphrase=PASS, passphrase2=PASS))
    assert ok.status_code == 303
    with portal.sm() as s:
        t = s.scalars(select(BackupTarget)).one()
        assert (t.owner_name, t.folder, t.hour, t.mode) == ("ada", "nas/ada", 2, "passphrase")
        assert PASS not in (t.recipient or "") and t.recipient.startswith("cpgbk1.")
        assert [a.action for a in s.scalars(select(AdminAudit)) if a.action.startswith("backup")] == ["backup.create"]
        assert PASS not in str([a.detail_json for a in s.scalars(select(AdminAudit))])
        tid = t.id

    run = await portal.post(f"/admin/backups/{tid}/run", data=dict(csrf_token=csrf))
    assert run.status_code == 303
    assert "Backup done" in (await portal.get("/admin/backups")).text
    files = [p for p in (dirs / "nas" / "ada").iterdir() if p.name.endswith(".tar.gz.enc")]
    assert len(files) == 1 and "-t%d-" % tid in files[0].name
    assert list((dirs / "nas" / "ada").glob("audit-archive-*/*.enc"))

    # another administrator sees it, runs it, but cannot change it
    await portal.post("/logout", data=dict(csrf_token=csrf))
    await portal_login(portal, "eli", GOOD)
    csrf2 = await csrf_of_page(portal, "/admin/backups")
    text = (await portal.get("/admin/backups")).text
    assert "NAS ada" in text and "belongs to ada" in text
    assert (await portal.get(f"/admin/backups/{tid}/edit")).status_code == 403
    assert (await portal.post(f"/admin/backups/{tid}/delete", data=dict(csrf_token=csrf2))).status_code == 403
    assert (await portal.post(f"/admin/backups/{tid}/toggle", data=dict(csrf_token=csrf2))).status_code == 403
    assert (await portal.post(f"/admin/backups/{tid}/run", data=dict(csrf_token=csrf2))).status_code == 303
    # and has an independent one of their own
    await portal.post("/admin/backups", data=dict(csrf_token=csrf2, name="USB eli", folder="usb", mode="none"))
    assert (dirs / "usb").is_dir()
    with portal.sm() as s:
        assert {t.owner_name for t in s.scalars(select(BackupTarget))} == {"ada", "eli"}

    # a plain user has no access at all
    await portal.post("/logout", data=dict(csrf_token=csrf2))
    await portal_login(portal, "pia", GOOD)
    assert (await portal.get("/admin/backups")).status_code == 403


async def test_owner_can_edit_toggle_and_delete(portal, dirs):
    make_user(portal.sm, "ada", password=GOOD, admin=True)
    await portal_login(portal, "ada", GOOD)
    csrf = await csrf_of_page(portal, "/admin/backups")
    await portal.post("/admin/backups", data=dict(csrf_token=csrf, name="one", folder="one", mode="none"))
    with portal.sm() as s:
        tid = s.scalars(select(BackupTarget)).one().id
    assert "Edit backup" in (await portal.get(f"/admin/backups/{tid}/edit")).text
    await portal.post(f"/admin/backups/{tid}", data=dict(csrf_token=csrf, name="two", folder="two", hour="5",
                                                         keep_daily="2", keep_weekly="0", keep_monthly="0"))
    await portal.post(f"/admin/backups/{tid}/toggle", data=dict(csrf_token=csrf))
    with portal.sm() as s:
        t = s.get(BackupTarget, tid)
        assert (t.name, t.folder, t.hour, t.enabled) == ("two", "two", 5, False)
    await portal.post(f"/admin/backups/{tid}/delete", data=dict(csrf_token=csrf))
    with portal.sm() as s:
        assert s.get(BackupTarget, tid) is None


async def test_a_failing_backup_is_visible_not_silent(portal, dirs):
    make_user(portal.sm, "ada", password=GOOD, admin=True)
    with portal.sm() as s:
        t = BackupTarget(owner_name="ada", name="broken", folder="x/../..", mode="none")
        s.add(t)
        s.commit()
        tid = t.id
    ok, msg = service.run_target(portal.sm, tid)
    assert not ok and "not valid" in msg
    with portal.sm() as s:
        t = s.get(BackupTarget, tid)
        assert t.last_status == "error" and t.last_error and t.last_ok_at is None
        assert service.health(t, datetime.now(UTC)) == "error"


async def test_unmounted_destination_raises_a_notice_with_the_real_name_and_it_closes_when_fixed(portal, dirs):
    from spoolsteward.db.models import Notice

    make_user(portal.sm, "ada", password=GOOD, admin=True)
    (dirs / "nas").mkdir()
    with portal.sm() as s:
        t = BackupTarget(owner_name="ada", name="NAS office", folder="nas", mode="none", require_marker=True)
        s.add(t)
        s.commit()
        tid = t.id
    ok, _msg = service.run_target(portal.sm, tid)       # the share is not mounted: no marker file
    assert not ok
    with portal.sm() as s:
        n = s.query(Notice).filter(Notice.kind == "backup_not_mounted", Notice.key == str(tid)).one()
        assert n.dismissed_at is None and n.detail_json["name"] == "NAS office"
    await portal_login(portal, "ada", GOOD)
    page = (await portal.get("/admin/notices")).text
    assert "NAS office" in page and "can not be reached" in page

    core.make_marker(dirs / "nas")                        # mounted again
    ok, _msg = service.run_target(portal.sm, tid)
    assert ok
    with portal.sm() as s:
        assert s.query(Notice).filter(Notice.kind == "backup_not_mounted", Notice.dismissed_at.is_(None)).count() == 0
        assert s.query(Notice).filter(Notice.kind == "backup_not_mounted").one().dismissed_by == "system"


async def test_a_new_backup_is_created_with_the_marker_ticked(portal, dirs):
    make_user(portal.sm, "ada", password=GOOD, admin=True)
    await portal_login(portal, "ada", GOOD)
    page = (await portal.get("/admin/backups")).text
    assert 'name="require_marker" value="1" checked' in page


# ------------------------------------------------------------------------------------------------ schedule

def test_schedule_runs_once_a_day_after_the_hour(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "UTC")
    t = BackupTarget(name="n", folder="f", enabled=True, hour=3, created_at=datetime(2026, 10, 1, tzinfo=UTC))
    at = lambda h, d=11: datetime(2026, 10, d, h, 30, tzinfo=UTC)  # noqa: E731
    assert not service.is_due(t, at(2))                  # before the hour
    assert service.is_due(t, at(3))                      # first time: as soon as the hour has come
    t.last_ok_at, t.last_status = at(3), "ok"
    assert not service.is_due(t, at(15))                 # done today
    assert service.is_due(t, at(3, d=12))                # next day
    t.last_ok_at, t.last_status, t.last_attempt_at = at(3, d=10), "error", at(3)
    assert not service.is_due(t, at(3))                  # failed a moment ago: wait before retrying
    t.last_attempt_at = at(1) - timedelta(hours=3)
    assert service.is_due(t, at(5))                      # retried after an hour
    t.enabled = False
    assert not service.is_due(t, at(5))


def test_health_flags_old_backups():
    now = datetime(2026, 10, 11, tzinfo=UTC)
    t = BackupTarget(name="n", folder="f", enabled=True, created_at=now - timedelta(days=5),
                     last_ok_at=now - timedelta(days=3), last_status="ok")
    assert service.health(t, now) == "stale"
    t.last_ok_at = now - timedelta(hours=5)
    assert service.health(t, now) == "ok"
    t.last_ok_at = None
    assert service.health(t, now) == "stale"             # enabled for days and never worked


async def test_general_backup_follows_gateway_env(portal, dirs, monkeypatch):
    monkeypatch.setattr(settings, "backup_enabled", True)
    monkeypatch.setattr(settings, "backup_dir", "general")
    monkeypatch.setattr(settings, "backup_recipient", crypto.make_recipient(PASS))
    with portal.sm() as s:
        service.sync_global(s)
        g = s.scalars(select(BackupTarget).where(BackupTarget.is_global)).one()
        assert g.enabled and g.mode == "key" and g.folder == "general"
    ok, msg = service.run_target(portal.sm, g.id)
    assert ok, msg
    assert any(p.name.endswith(".enc") for p in (dirs / "general").iterdir())
    monkeypatch.setattr(settings, "backup_enabled", False)
    with portal.sm() as s:
        service.sync_global(s)
        assert not s.scalars(select(BackupTarget).where(BackupTarget.is_global)).one().enabled
    # the portal shows it as read-only
    make_user(portal.sm, "ada", password=GOOD, admin=True)
    await portal_login(portal, "ada", GOOD)
    text = (await portal.get("/admin/backups")).text
    assert "edited in .env" in text
    assert (await portal.get(f"/admin/backups/{g.id}/edit")).status_code == 403


async def test_run_now_adds_a_copy_and_only_the_schedule_removes_old_ones(portal, dirs):
    make_user(portal.sm, "ada", password=GOOD, admin=True)
    with portal.sm() as s:
        t = BackupTarget(owner_name="ada", name="n", folder="x", mode="none", keep_daily=1, keep_weekly=0, keep_monthly=0)
        s.add(t)
        s.commit()
        tid = t.id
    for _ in range(3):
        ok, msg = service.run_target(portal.sm, tid, prune=False)
        assert ok, msg
        import time
        time.sleep(1.1)                                   # file names carry the second
    f = core.resolve_folder("x")
    with portal.sm() as s:
        inst = service.instance_id(s)
    assert len(core.list_backups(f, inst, tid)) == 3      # three clicks, three copies
    ok, _ = service.run_target(portal.sm, tid)            # the scheduled one tidies up: one per day is kept
    assert ok and len(core.list_backups(f, inst, tid)) == 1


def test_marker_required_refuses_unmounted_folder(tmp_path):
    from spoolsteward.backup import core

    folder = tmp_path / "nas"
    folder.mkdir()
    with pytest.raises(core.BackupError):
        core.check_marker(folder)  # an empty mount point: the share is not there
    core.make_marker(folder)
    core.check_marker(folder)


async def test_update_button_needs_updater_and_a_working_scheduled_backup(portal, dirs, tmp_path, monkeypatch):
    import json

    from spoolsteward import updates

    udir = tmp_path / "update"
    udir.mkdir()
    monkeypatch.setattr(settings, "update_dir", str(udir))
    monkeypatch.setattr(updates, "app_version", lambda: "1.0.0")
    make_user(portal.sm, "ada", password=GOOD, admin=True)
    await portal_login(portal, "ada", GOOD)
    csrf = await csrf_of_page(portal, "/admin/backups")
    portal.app.state.update_info = {"version": "1.1.0", "url": ""}

    # no updater container and no backup: the button is off
    page = (await portal.get("/admin/backups")).text
    assert "<button disabled>" in page
    assert (await portal.post("/admin/update", data=dict(csrf_token=csrf))).status_code == 303
    assert not (udir / "request.json").exists()

    # a working scheduled backup, but still no updater
    await portal.post("/admin/backups", data=dict(csrf_token=csrf, name="n", folder="n", mode="none"))
    with portal.sm() as s:
        t = s.scalars(select(BackupTarget)).one()
        t.last_ok_at, t.last_status = datetime.now(UTC), "ok"
        s.commit()
    assert not (udir / "request.json").exists() and "<button disabled>" in (await portal.get("/admin/backups")).text

    # updater alive: the button works, a fresh backup is taken and the request holds only the version
    (udir / "heartbeat").write_text("")
    page = (await portal.get("/admin/backups")).text
    assert "<button disabled>" not in page and 'action="/admin/update"' in page
    await portal.post("/admin/update", data=dict(csrf_token=csrf))
    req = json.loads((udir / "request.json").read_text())
    assert req["version"] == "1.1.0" and req["by"] == "ada" and req["backup"].endswith(".tar.gz")
    assert list((dirs / "n").glob("*.tar.gz"))
    with portal.sm() as s:
        assert "update.request" in [a.action for a in s.scalars(select(AdminAudit))]


def test_update_request_refuses_other_majors_and_junk(tmp_path, monkeypatch):
    from spoolsteward import updates

    monkeypatch.setattr(settings, "update_dir", str(tmp_path))
    monkeypatch.setattr(updates, "app_version", lambda: "1.4.0")
    for bad in ("2.0.0", "1.5", "1.5.0; reboot", "../1.5.0"):
        with pytest.raises(ValueError):
            updates.write_request(bad, "ada", "b.tar.gz")
    updates.write_request("1.5.0", "ada", "b.tar.gz")
    assert (tmp_path / "request.json").exists()
    (tmp_path / "status.json").write_text('{"state":"ok","version":"1.5.0","backup":"/etc/passwd"}')
    assert updates.read_status()["backup"] == "passwd"        # only a file name is ever shown
    (tmp_path / "status.json").write_text('{"state":"<script>","version":"1.5.0"}')
    assert updates.read_status() == {}


def test_bounded_gives_up_on_a_stuck_disk_and_does_not_pile_up():
    import threading
    import time

    release = threading.Event()
    started = time.monotonic()
    with pytest.raises(core.BackupError):
        core.bounded(release.wait, 30, timeout=0.3)         # a disk that never answers
    assert time.monotonic() - started < 3
    for _i in range(3):                                     # fill the remaining slots with stuck calls
        with pytest.raises(core.BackupError):
            core.bounded(release.wait, 30, timeout=0.1)
    with pytest.raises(core.BackupError, match="earlier requests"):
        core.bounded(lambda: 1, timeout=0.1)                # all slots stuck: refused at once, no new thread
    release.set()
    time.sleep(0.3)
    assert core.bounded(lambda: 42) == 42                   # the disk is back: works again



def test_restore_audit_with_a_wrong_passphrase_leaves_no_half_file(tmp_path):
    src = tmp_path / "archive"
    src.mkdir()
    (src / "2026-01.jsonl.gz").write_bytes(b"some audit bytes" * 1000)
    folder = tmp_path / "bk"
    folder.mkdir()
    core.mirror_audit(src, folder, "inst", crypto.make_recipient(PASS))
    dest = tmp_path / "restored"
    with pytest.raises(crypto.CryptoError):
        core.restore_audit(folder / "audit-archive-inst", dest, "wrong pass phrase")
    assert list(dest.iterdir()) == []


async def test_a_backup_that_fails_while_writing_leaves_no_file_behind(portal, dirs, monkeypatch):
    f = dirs
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(core, "_add", boom)
    with pytest.raises(OSError):
        core.create_backup(portal.sm, f, "spoolsteward-aaaaaaaa-t1", recipient=crypto.make_recipient(PASS), target_name="e",
                           instance="aaaaaaaa")
    assert [p.name for p in f.iterdir()] == []
