"""Pins what every `python -m spoolsteward.cli` command prints, returns and changes, so the command bodies can be moved around safely."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from spoolsteward import cli
from spoolsteward.config import settings
from spoolsteward.db.models import (AdminAudit, Group, JobKind, JobLog, JobState, Metric, Period, PolicyRule, SubjectType, User)
from tests.conftest import make_user


@pytest.fixture
def db(portal, monkeypatch):
    monkeypatch.setattr(cli, "SessionLocal", portal.sm)
    monkeypatch.setattr(cli, "init_db", lambda: None)
    return portal.sm


def run(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def user_of(sm, name):
    with sm() as s:
        return s.scalars(select(User).where(User.username == name)).first()


def rules(sm):
    with sm() as s:
        return [(r.subject_type, r.subject_id, r.metric, r.limit, r.period) for r in s.scalars(select(PolicyRule).order_by(PolicyRule.id))]


def group_id(sm, name):
    with sm() as s:
        g = s.scalars(select(Group).where(Group.name == name)).first()
        return g.id if g else None


# ---------------------------------------------------------------- limits

def test_group_limit_creates_the_group_and_the_rule(db, capsys):
    assert run(["group-limit", "staff", "50"], capsys) == (0, "", "")
    gid = group_id(db, "staff")
    assert gid is not None and rules(db) == [(SubjectType.group, gid, Metric.jobs, 50, Period.weekly)]


def test_group_limit_with_metric_period_and_none(db, capsys):
    assert run(["group-limit", "staff", "100", "--metric", "pages", "--period", "monthly"], capsys) == (0, "", "")
    gid = group_id(db, "staff")
    assert rules(db) == [(SubjectType.group, gid, Metric.pages, 100, Period.monthly)]
    assert run(["group-limit", "staff", "NoNe", "--metric", "pages"], capsys) == (0, "", "")
    assert rules(db) == []


def test_user_limit_for_a_known_user(db, capsys):
    uid = make_user(db, "ann", password="correct horse battery", group="staff")
    assert run(["user-limit", "Ann ", "7"], capsys) == (0, "", "")
    assert rules(db) == [(SubjectType.user, uid, Metric.jobs, 7, Period.weekly)]


def test_user_limit_for_an_unknown_user_fails(db, capsys):
    assert run(["user-limit", "ghost", "7"], capsys) == (1, "", "unknown user (created at first login, or use make-admin)\n")
    assert rules(db) == []


def test_limit_out_of_range_raises(db, capsys):
    make_user(db, "ann", password="correct horse battery")
    with pytest.raises(ValueError):
        cli.main(["user-limit", "ann", "-1"])
    with pytest.raises(ValueError):
        cli.main(["group-limit", "staff", "abc"])


def test_limit_arguments_are_validated_by_argparse(db, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["group-limit", "staff", "5", "--metric", "bogus"])
    assert e.value.code == 2
    assert "invalid choice: 'bogus'" in capsys.readouterr().err


def test_a_command_is_required(db, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 2
    assert "the following arguments are required: cmd" in capsys.readouterr().err


# ---------------------------------------------------------------- set-group

def test_set_group_moves_the_user_and_creates_the_group(db, capsys):
    make_user(db, "ann", password="correct horse battery", group="staff")
    assert run(["set-group", "ann", "newgroup"], capsys) == (0, "", "")
    assert user_of(db, "ann").group_id == group_id(db, "newgroup")


def test_set_group_unknown_user(db, capsys):
    assert run(["set-group", "ghost", "staff"], capsys) == (1, "", "unknown user\n")
    assert group_id(db, "staff") is None


def test_set_group_refuses_the_emergency_administrator(db, capsys):
    make_user(db, settings.bootstrap_admin_username, password="correct horse battery", admin=True)
    code, out, err = run(["set-group", settings.bootstrap_admin_username, "staff"], capsys)
    assert (code, out) == (1, "")
    assert err == f"{settings.bootstrap_admin_username} is the emergency administrator: it has no group\n"
    assert user_of(db, settings.bootstrap_admin_username).group_id is None


# ---------------------------------------------------------------- make-admin

def test_make_admin_promotes_an_existing_user(db, capsys):
    make_user(db, "ann", password="correct horse battery", group="staff")
    assert run(["make-admin", "ANN"], capsys) == (0, "", "")
    u = user_of(db, "ann")
    assert u.is_admin and u.group_id == group_id(db, "administrator")


def test_make_admin_cannot_create_without_radius(db, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["make-admin", "newbie"])
    assert e.value.code == "RADIUS is not configured: create the account with set-admin-password (local account)"
    assert user_of(db, "newbie") is None


def test_make_admin_creates_a_radius_user_when_radius_is_configured(db, capsys, monkeypatch):
    monkeypatch.setattr(settings, "radius_host", "radius.test")
    monkeypatch.setattr(settings, "radius_secret", "s3cret")
    assert run(["make-admin", "Newbie"], capsys) == (0, "", "")
    u = user_of(db, "newbie")
    assert u.provider == "radius" and u.is_admin and u.group_id == group_id(db, "administrator")


# ---------------------------------------------------------------- set-admin-password

def feed(monkeypatch, *answers):
    asked = []
    it = iter(answers)

    def fake(prompt=""):
        asked.append(prompt)
        return next(it)
    monkeypatch.setattr(cli.getpass, "getpass", fake)
    return asked


def test_set_admin_password_for_a_radius_account_is_refused(db, capsys, monkeypatch):
    make_user(db, "rad", provider="radius")
    asked = feed(monkeypatch)
    assert run(["set-admin-password", "rad"], capsys) == (1, "", "rad is a RADIUS account: its password is managed there\n")
    assert asked == []


def test_set_admin_password_mismatch(db, capsys, monkeypatch):
    asked = feed(monkeypatch, "correct horse battery", "something else")
    assert run(["set-admin-password", "Boss"], capsys) == (1, "", "the two passwords differ\n")
    assert asked == ["New password for boss: ", "Repeat: "]
    assert user_of(db, "boss") is None


def test_set_admin_password_weak(db, capsys, monkeypatch):
    feed(monkeypatch, "short", "short")
    code, out, err = run(["set-admin-password", "boss"], capsys)
    assert (code, out) == (1, "") and err.endswith("\n") and len(err) > 1
    assert user_of(db, "boss") is None


def test_set_admin_password_creates_a_local_admin(db, capsys, monkeypatch):
    feed(monkeypatch, "correct horse battery", "correct horse battery")
    assert run(["set-admin-password", "Boss"], capsys) == (0, "password set for boss\n", "")
    u = user_of(db, "boss")
    assert u.provider == "local" and u.is_admin and u.enabled and not u.must_change_password
    assert u.password_hash and u.password_hash != "correct horse battery" and u.password_changed_at is not None
    with db() as s:
        a = s.scalars(select(AdminAudit).where(AdminAudit.action == "admin.set_password")).one()
        assert a.target_id == u.id and a.detail_json["username"] == "boss" and a.detail_json["via"] == "cli"


def test_set_admin_password_updates_an_existing_local_account(db, capsys, monkeypatch):
    make_user(db, "ann", password="old password here", enabled=False, must_change=True, group="staff")
    old = user_of(db, "ann").password_hash
    feed(monkeypatch, "correct horse battery", "correct horse battery")
    assert run(["set-admin-password", "ann"], capsys) == (0, "password set for ann\n", "")
    u = user_of(db, "ann")
    assert u.password_hash != old and u.enabled and not u.must_change_password
    assert not u.is_admin            # an existing account is not promoted


def test_set_admin_password_default_username(db, capsys, monkeypatch):
    feed(monkeypatch, "correct horse battery", "correct horse battery")
    name = settings.bootstrap_admin_username.strip().lower()
    assert run(["set-admin-password"], capsys) == (0, f"password set for {name}\n", "")
    assert user_of(db, name).is_admin


# ---------------------------------------------------------------- jobs

def add_job(sm, uid, ts, title, state=JobState.completed, printer="p1", pages=None, denied=None):
    with sm() as s:
        s.add(JobLog(ts=ts, user_id=uid, kind=JobKind.print, printer=printer, title=title, pages=pages, state=state,
                     denied_reason=denied, cups_job_id=1))
        s.commit()


def test_jobs_with_nothing_prints_nothing(db, capsys):
    assert run(["jobs"], capsys) == (0, "", "")


def test_jobs_prints_oldest_first_and_honours_n(db, capsys):
    uid = make_user(db, "ann", password="correct horse battery")
    add_job(db, uid, datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC), "first.pdf", pages=3)
    add_job(db, uid, datetime(2026, 1, 2, 10, 0, 0, tzinfo=UTC), "second.pdf", state=JobState.denied, printer=None, denied="quota")
    add_job(db, None, datetime(2026, 1, 3, 10, 0, 0, tzinfo=UTC), "third.pdf")
    code, out, err = run(["jobs"], capsys)
    assert (code, err) == (0, "")
    assert out == (
        f"2026-01-01 10:00:00Z {'ann':<12} {'completed':<9} {'p1':<12} 'first.pdf' 3p\n"
        f"2026-01-02 10:00:00Z {'ann':<12} {'denied':<9} {'-':<12} 'second.pdf'  [quota]\n"
        f"2026-01-03 10:00:00Z {'-':<12} {'completed':<9} {'p1':<12} 'third.pdf'\n")
    code, out, err = run(["jobs", "-n", "1"], capsys)
    assert out == f"2026-01-03 10:00:00Z {'-':<12} {'completed':<9} {'p1':<12} 'third.pdf'\n"


# ---------------------------------------------------------------- backup-run

def test_backup_run_without_enabled_targets(db, capsys, monkeypatch):
    monkeypatch.setattr(settings, "backup_enabled", False)
    assert run(["backup-run"], capsys) == (0, "no enabled backup: set one up in the portal (Backup) or in .env\n", "")


def test_backup_run_runs_the_enabled_ones_and_reports_failures(db, capsys, monkeypatch):
    from spoolsteward.backup import service
    from spoolsteward.db.models import BackupTarget

    with db() as s:
        s.add_all([BackupTarget(name="a", folder="a", enabled=True), BackupTarget(name="b", folder="b", enabled=False),
                   BackupTarget(name="c", folder="c", enabled=True)])
        s.commit()
        ids = [t.id for t in s.scalars(select(BackupTarget).order_by(BackupTarget.id))]
    monkeypatch.setattr(settings, "backup_enabled", False)
    calls = []

    def fake_run(sm, tid):
        calls.append(tid)
        return (tid == ids[0]), f"msg {tid}"
    monkeypatch.setattr(service, "run_target", fake_run)
    assert run(["backup-run"], capsys) == (1, f"OK   #{ids[0]}: msg {ids[0]}\nFAIL #{ids[2]}: msg {ids[2]}\n", "")
    assert calls == [ids[0], ids[2]]
    calls.clear()
    assert run(["backup-run", "--id", str(ids[0])], capsys) == (0, f"OK   #{ids[0]}: msg {ids[0]}\n", "")
    assert calls == [ids[0]]


# ---------------------------------------------------------------- backup-key / verify / restore

def test_backup_key_prints_the_recipient_and_a_hint(db, capsys, monkeypatch):
    from spoolsteward.backup import crypto

    asked = feed(monkeypatch, "a long enough passphrase 1", "a long enough passphrase 1")
    monkeypatch.setattr(crypto, "make_recipient", lambda pw: f"RECIPIENT({pw})")
    code, out, err = run(["backup-key"], capsys)
    assert (code, out) == (0, "RECIPIENT(a long enough passphrase 1)\n")
    assert err.startswith("\nPut it in .env as SPOOLSTEWARD_BACKUP_RECIPIENT=<the line above> or paste it in the portal ")
    assert err.endswith("without it the backups cannot be opened.\n")
    assert asked == ["Backup passphrase: ", "Repeat: "]


def test_backup_key_passphrases_differ(db, capsys, monkeypatch):
    feed(monkeypatch, "one", "two")
    with pytest.raises(SystemExit) as e:
        cli.main(["backup-key"])
    assert e.value.code == "the two passphrases differ"


def test_backup_key_weak_passphrase_is_an_error(db, capsys, monkeypatch):
    feed(monkeypatch, "x", "x")
    code, out, err = run(["backup-key"], capsys)
    assert (code, out) == (1, "") and err.startswith("error: ")


def test_backup_verify_and_restore_missing_file(db, capsys, tmp_path):
    missing = tmp_path / "nope.tar"
    assert run(["backup-verify", str(missing)], capsys) == (1, "", f"{missing}: no such file\n")
    assert run(["backup-restore", str(missing)], capsys) == (1, "", f"{missing}: no such file\n")


def test_backup_verify_plain_and_encrypted(db, capsys, monkeypatch, tmp_path):
    from spoolsteward.backup import core

    seen = []

    def fake_verify(path, pw):
        seen.append((path, pw))
        return {"files": [1, 2], "created": "2026-01-01", "app_version": "9.9", "encrypted": pw is not None}
    monkeypatch.setattr(core, "verify", fake_verify)
    plain, enc = tmp_path / "b.tar", tmp_path / "b.tar.enc"
    plain.write_bytes(b"x")
    enc.write_bytes(b"x")
    asked = feed(monkeypatch, "pw")
    assert run(["backup-verify", str(plain)], capsys) == (0, "OK: 2 part(s), created 2026-01-01, version 9.9, not encrypted\n", "")
    assert asked == []
    assert run(["backup-verify", str(enc)], capsys) == (0, "OK: 2 part(s), created 2026-01-01, version 9.9, encrypted\n", "")
    assert asked == ["Backup passphrase: "]
    assert seen == [(plain, None), (enc, "pw")]


def test_backup_verify_reports_backup_errors(db, capsys, monkeypatch, tmp_path):
    from spoolsteward.backup import core

    def boom(path, pw):
        raise core.BackupError("corrupt archive")
    monkeypatch.setattr(core, "verify", boom)
    f = tmp_path / "b.tar"
    f.write_bytes(b"x")
    assert run(["backup-verify", str(f)], capsys) == (1, "", "error: corrupt archive\n")


def test_backup_restore_needs_sqlite(db, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_url", "postgresql://u:p@localhost/db")
    f = tmp_path / "b.tar"
    f.write_bytes(b"x")
    assert run(["backup-restore", str(f)], capsys) == (1, "", "restore works on SQLite databases only\n")


def test_backup_restore_with_and_without_configuration(db, capsys, monkeypatch, tmp_path):
    from spoolsteward.backup import core

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'x.db'}")
    got = []
    result = {"manifest": {"created": "2026-02-02"}, "audit_files": 3, "config": True}

    def fake_restore(path, pw, *, db_path, config_out, audit_dir):
        got.append((path, pw, db_path, config_out, audit_dir))
        return result
    monkeypatch.setattr(core, "restore", fake_restore)
    f = tmp_path / "b.tar"
    f.write_bytes(b"x")
    out_dir = tmp_path / "cfg"
    code, out, err = run(["backup-restore", str(f), "--config-out", str(out_dir)], capsys)
    assert (code, err) == (0, "")
    assert out == (f"database restored (2026-02-02); 3 audit file(s) brought back\n"
                   f"configuration files are in {out_dir}: copy .env into the project folder and tls/ into ./config on the host "
                   "if this is a new machine, then start the gateway\n")
    assert got == [(f, None, tmp_path / "x.db", out_dir, Path(settings.audit_archive_dir))]
    result["config"] = False
    assert run(["backup-restore", str(f), "--config-out", str(out_dir)], capsys) == (
        0, "database restored (2026-02-02); 3 audit file(s) brought back\n", "")


def test_backup_restore_default_config_out(db, capsys, monkeypatch, tmp_path):
    from spoolsteward.backup import core

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'x.db'}")
    got = []
    monkeypatch.setattr(core, "restore", lambda *a, **k: got.append(k["config_out"]) or {"manifest": {"created": "c"}, "audit_files": 0, "config": False})
    f = tmp_path / "b.tar"
    f.write_bytes(b"x")
    assert run(["backup-restore", str(f)], capsys)[0] == 0
    assert got == [Path("/data/restored-config")]


def test_offline_commands_do_not_open_the_database(portal, capsys, monkeypatch, tmp_path):
    def boom():
        raise AssertionError("init_db must not run")
    monkeypatch.setattr(cli, "init_db", boom)
    missing = tmp_path / "nope"
    assert run(["backup-verify", str(missing)], capsys)[0] == 1
