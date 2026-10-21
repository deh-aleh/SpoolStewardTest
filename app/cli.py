"""Admin CLI: python -m app.cli <command>. Also used to bootstrap the first admin."""

import argparse
import getpass
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.audit.adminlog import record
from app.audit.archive import archive_all
from app.auth.groups import set_admin
from app.auth.users import is_emergency
from app.auth.passwords import hash_password, validate_new_password
from app.db.models import Group, JobLog, Metric, Period, SubjectType, User
from app.db.session import SessionLocal, init_db
from app.policy.rules import set_limit


def get_group(s, name):
    g = s.scalars(select(Group).where(Group.name == name)).first()
    if g is None:
        g = Group(name=name)
        s.add(g)
        s.flush()
    return g


def get_user(s, username, create=False):
    name = username.strip().lower()
    u = s.scalars(select(User).where(User.username == name)).first()
    if u is None and create:
        if not settings.radius_enabled:
            raise SystemExit("RADIUS is not configured: create the account with set-admin-password (local account)")
        u = User(provider="radius", username=name, group_id=get_group(s, settings.default_group).id)
        s.add(u)
        s.flush()
    return u


def _passphrase(path: Path | None = None, confirm: bool = False) -> str | None:
    if path is not None and not path.name.endswith(".enc"):
        return None
    pw = getpass.getpass("Backup passphrase: ")
    if confirm and pw != getpass.getpass("Repeat: "):
        raise SystemExit("the two passphrases differ")
    return pw


def _backup_offline(args) -> int:
    """Commands that must work without touching the live database (and while the gateway is stopped)."""
    from app.backup import core, crypto

    try:
        if args.cmd == "backup-key":
            print(crypto.make_recipient(_passphrase(confirm=True)))
            print("\nPut it in .env as SPOOLSTEWARD_BACKUP_RECIPIENT=<the line above> or paste it in the portal "
                  "(Backup > New backup > ready-made recovery key). Keep the passphrase: without it the backups cannot be opened.", file=sys.stderr)
            return 0
        path = Path(args.file)
        if not path.is_file():
            print(f"{path}: no such file", file=sys.stderr)
            return 1
        pw = _passphrase(path)
        if args.cmd == "backup-verify":
            m = core.verify(path, pw)
            print(f"OK: {len(m['files'])} part(s), created {m['created']}, version {m['app_version']}, "
                  f"{'encrypted' if m['encrypted'] else 'not encrypted'}")
            return 0
        from sqlalchemy.engine import make_url

        url = make_url(settings.database_url)
        if url.get_backend_name() != "sqlite" or not url.database:
            print("restore works on SQLite databases only", file=sys.stderr)
            return 1
        r = core.restore(path, pw, db_path=Path(url.database), config_out=Path(args.config_out),
                         audit_dir=Path(settings.audit_archive_dir))
        print(f"database restored ({r['manifest']['created']}); {r['audit_files']} audit file(s) brought back")
        if r["config"]:
            print(f"configuration files are in {args.config_out}: copy .env into the project folder and tls/ into ./config on the host "
                  "if this is a new machine, then start the gateway")
        return 0
    except (core.BackupError, crypto.CryptoError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd, who in (("group-limit", "group"), ("user-limit", "username")):
        p = sub.add_parser(cmd, help=f"set a {who} limit; LIMIT 'none' removes it")
        p.add_argument(who); p.add_argument("limit")
        p.add_argument("--metric", choices=[m.value for m in Metric], default="jobs")
        p.add_argument("--period", choices=[m.value for m in Period], default="weekly")
    p = sub.add_parser("set-group", help="move a user to a group (creates the group)")
    p.add_argument("username"); p.add_argument("group")
    p = sub.add_parser("make-admin", help="grant admin rights (creates the user if needed)")
    p.add_argument("username")
    p = sub.add_parser("set-admin-password", help="create/update a LOCAL administrator and set its password")
    p.add_argument("username", nargs="?", default=settings.bootstrap_admin_username)
    p = sub.add_parser("audit-archive", help="archive old audit rows now")
    p.add_argument("--force", action="store_true", help="ignore the size threshold (rows in the protected window stay)")
    p = sub.add_parser("jobs", help="show the audit log"); p.add_argument("-n", type=int, default=20)
    sub.add_parser("backup-key", help="make a backup recovery key from a passphrase (for SPOOLSTEWARD_BACKUP_RECIPIENT or the portal)")
    p = sub.add_parser("backup-run", help="run the enabled backups now (all, or one id)"); p.add_argument("--id", type=int)
    p = sub.add_parser("backup-verify", help="check a backup file (asks the passphrase when encrypted)"); p.add_argument("file")
    p = sub.add_parser("backup-restore", help="restore the database and audit files from a backup (stop the gateway first)")
    p.add_argument("file")
    p.add_argument("--config-out", default="/data/restored-config", help="where the configuration files of the backup are put")
    return ap


# The commands below work on the open session `s`. They return an exit code to stop at once (nothing is committed),
# or None to commit and finish with 0.

def _cmd_limit(s, args) -> int | None:
    limit = None if args.limit.lower() == "none" else int(args.limit)
    if args.cmd == "group-limit":
        stype, sid = SubjectType.group, get_group(s, args.group).id
    else:
        u = get_user(s, args.username)
        if u is None:
            print("unknown user (created at first login, or use make-admin)", file=sys.stderr)
            return 1
        stype, sid = SubjectType.user, u.id
    set_limit(s, stype, sid, Metric(args.metric), limit, Period(args.period))
    return None


def _cmd_set_group(s, args) -> int | None:
    u = get_user(s, args.username)
    if u is None:
        print("unknown user", file=sys.stderr)
        return 1
    if is_emergency(u):
        print(f"{u.username} is the emergency administrator: it has no group", file=sys.stderr)
        return 1
    u.group_id = get_group(s, args.group).id
    return None


def _cmd_set_admin_password(s, args) -> int | None:
    name = args.username.strip().lower()
    u = s.scalars(select(User).where(User.username == name)).first()
    if u is not None and u.provider != "local":
        print(f"{name} is a RADIUS account: its password is managed there", file=sys.stderr)
        return 1
    pw = getpass.getpass(f"New password for {name}: ")
    if pw != getpass.getpass("Repeat: "):
        print("the two passwords differ", file=sys.stderr)
        return 1
    error = validate_new_password(pw, name)
    if error:
        print(error, file=sys.stderr)
        return 1
    if u is None:
        u = User(provider="local", username=name)
        s.add(u)
        s.flush()
        set_admin(s, u, True)
    u.password_hash, u.must_change_password, u.enabled = hash_password(pw), False, True
    u.password_changed_at = datetime.now(UTC)
    s.flush()
    record(s, None, "admin.set_password", "user", u.id, username=name, via="cli")
    print(f"password set for {name}")
    return None


def _cmd_audit_archive(s, args) -> int | None:
    done = archive_all(SessionLocal, force=args.force)
    for r in done:
        print(f"{r.table}: {r.rows} rows -> {r.path} ({r.bytes} bytes, sha256 {r.sha256[:12]}…)")
    if not done:
        print(f"nothing to archive (threshold {settings.audit_max_mb} MB; use --force to ignore it)")
    return None


def _cmd_backup_run(s, args) -> int | None:
    from sqlalchemy import select as _select

    from app.backup import service
    from app.db.models import BackupTarget

    service.sync_global(s)
    ids = [args.id] if args.id else [t.id for t in s.scalars(_select(BackupTarget)) if t.enabled]
    if not ids:
        print("no enabled backup: set one up in the portal (Backup) or in .env")
    failed = 0
    for tid in ids:
        ok, msg = service.run_target(SessionLocal, tid)
        print(f"{'OK  ' if ok else 'FAIL'} #{tid}: {msg}")
        failed += not ok
    return 1 if failed else 0


def _cmd_make_admin(s, args) -> int | None:
    set_admin(s, get_user(s, args.username, create=True), True)
    return None


def _cmd_jobs(s, args) -> int | None:
    rows = s.execute(
        select(JobLog, User.username).join(User, User.id == JobLog.user_id, isouter=True)
        .order_by(JobLog.id.desc()).limit(args.n)
    ).all()
    for j, name in reversed(rows):
        extra = f"  [{j.denied_reason}]" if j.denied_reason else ""
        pages = f" {j.pages}p" if j.pages else ""
        print(f"{j.ts:%Y-%m-%d %H:%M:%S}Z {name or '-':<12} {j.state.value:<9} "
              f"{j.printer or '-':<12} {j.title!r}{pages}{extra}")
    return None


_COMMANDS = {
    "group-limit": _cmd_limit, "user-limit": _cmd_limit, "set-group": _cmd_set_group,
    "set-admin-password": _cmd_set_admin_password, "audit-archive": _cmd_audit_archive,
    "backup-run": _cmd_backup_run, "make-admin": _cmd_make_admin, "jobs": _cmd_jobs,
}


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd in ("backup-key", "backup-verify", "backup-restore"):
        return _backup_offline(args)

    init_db()
    with SessionLocal() as s:
        code = _COMMANDS[args.cmd](s, args)
        if code is not None:
            return code
        s.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
