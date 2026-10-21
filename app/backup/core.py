"""Making, checking, pruning and restoring backups. No database models here: the scheduler (service.py) and the CLI
use these functions; the folder, the encryption and the retention are decided by the caller.

A backup is one file `spoolsteward-<instance>-t<target>-<UTC stamp>.tar.gz` (plus `.enc` when encrypted) holding:
  MANIFEST.json   what is inside, with sha256 and size of each part, app version, schema revision
  spoolsteward.db      a consistent snapshot of the database (SQLite's own backup call: safe while people are printing)
  config/...      .env (all the settings) and the TLS certificate+key, ONLY when the backup is encrypted (they are secrets)
The compressed audit files are immutable and can be large, so they are not repeated in every backup: they are
copied once into `audit-archive-<instance>/` next to the backups (encrypted the same way).
Every file is written as *.partial, re-read and verified, and only then renamed: a crash never leaves a half
backup that looks good, and pruning of old ones happens only after a new one is verified.
"""

import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from zoneinfo import ZoneInfo

from app.i18n import _
from app.backup.crypto import CHUNK, MAGIC, CryptoError, DecryptReader, EncryptWriter
from app.config import BACKUP_FOLDER_RE, settings

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^spoolsteward-(?P<inst>[0-9a-f]{8})-t(?P<tid>\d+)-(?P<ts>\d{8}-\d{6})\.tar\.gz(?P<enc>\.enc)?$")
FOLDER_RE = BACKUP_FOLDER_RE   # one rule, also checked for SPOOLSTEWARD_BACKUP_DIR at start (app/config.py)
MAX_CONFIG_FILE = 1 << 20
MAX_CONFIG_FILES = 50
MANIFEST = "MANIFEST.json"


class BackupError(RuntimeError):
    pass


class NotMounted(BackupError):
    """The marker file of a network or USB destination is missing: the disk is probably not mounted."""


@dataclass
class Result:
    path: Path
    size: int
    mirrored: int = 0
    removed: list[str] = field(default_factory=list)


def tag(instance: str, target_id: int) -> str:
    return f"spoolsteward-{instance}-t{target_id}"


def resolve_folder(sub: str, create: bool = True) -> Path:
    """The folder an administrator typed, as a real path under the backup root. Anything that could leave the
    root (.., absolute paths, odd characters, a symlink pointing outside) is refused."""
    sub = (sub or "").strip().rstrip("/")
    if not FOLDER_RE.match(sub):
        raise BackupError(_("Folder name not valid: letters, digits, space . _ - and / for sub-folders (max 4 levels)"))
    root = Path(settings.backup_root)
    if not root.is_dir():
        raise BackupError(_("Backup root %(root)s is not available: mount a folder there (see docker-compose.yml)", root=root))
    real_root = root.resolve()
    path = (real_root / sub).resolve()
    if real_root not in path.parents:
        raise BackupError(_("Folder is outside the backup root"))
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path, prefix=".write-test-"):
                pass
        except OSError as exc:
            raise BackupError(_("Folder %(folder)s is not writable by the gateway: %(error)s", folder=sub, error=exc.strerror or exc)) from exc
    return path


class DiskNotAnswering(BackupError):
    """The destination did not answer in time (a network disk that is down or very slow)."""


_SLOTS = threading.BoundedSemaphore(4)


def bounded(fn, *args, timeout: float = 6.0):
    """Run `fn` (anything that touches the backup folder) and stop waiting after `timeout` seconds.
    A network disk that stopped answering blocks the thread that touches it for minutes; this keeps the portal and the
    scheduler responsive. At most 4 such calls wait at once: when all are stuck, new ones fail at once instead of piling up."""
    if not _SLOTS.acquire(blocking=False):
        raise DiskNotAnswering(_("The backup disk is not answering (earlier requests are still waiting). Check the network disk."))
    box: list = []

    def work():
        try:
            box.append((True, fn(*args)))
        except BaseException as exc:  # handed back to the caller below
            box.append((False, exc))
        finally:
            _SLOTS.release()

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if not box:
        raise DiskNotAnswering(_("The backup disk did not answer in %(s)s seconds: is the network disk reachable?", s=int(timeout)))
    ok, value = box[0]
    if ok:
        return value
    raise value


MARKER = ".spoolsteward-destination"


def make_marker(folder: Path) -> None:
    """Called while the destination is known to be mounted: from now on a backup refuses to write if the file is gone."""
    try:
        (folder / MARKER).write_text("SpoolSteward backup destination. Do not delete: backups refuse to run without it.\n")
    except OSError as exc:
        raise BackupError(_("Cannot write the marker file in the folder: %(error)s", error=exc.strerror or exc)) from exc


def check_marker(folder: Path) -> None:
    if not (folder / MARKER).is_file():
        raise NotMounted(_("The marker file is missing in this folder: the network disk or USB disk may not be mounted. Nothing was written."))


def existing_folders() -> list[str]:
    """Sub-folders already present under the root (to suggest in the form), two levels deep."""
    root = Path(settings.backup_root)
    out: list[str] = []
    try:
        for p in sorted(root.iterdir()):
            if p.is_dir() and not p.is_symlink() and FOLDER_RE.match(p.name):
                out.append(p.name)
                for q in sorted(p.iterdir()):
                    if q.is_dir() and not q.is_symlink() and FOLDER_RE.match(q.name):
                        out.append(f"{p.name}/{q.name}")
    except OSError:
        pass
    return out


def app_version() -> str:
    try:
        return metadata.version("spoolsteward")
    except metadata.PackageNotFoundError:
        return "unknown"


def known_revisions() -> set[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "migrations"))
    return {r.revision for r in ScriptDirectory.from_config(cfg).walk_revisions()}


def snapshot_db(sm, dest: Path) -> str | None:
    """Consistent copy of the database into `dest`. Returns the schema revision it carries."""
    bind = sm.kw.get("bind")
    if bind is None or bind.dialect.name != "sqlite":
        raise BackupError("Built-in backups support SQLite only; with another database use its own dump tool")
    raw = bind.raw_connection()
    try:
        out = sqlite3.connect(str(dest))
        try:
            raw.driver_connection.backup(out)
            try:
                row = out.execute("select version_num from alembic_version").fetchone()
            except sqlite3.Error:
                row = None
            return row[0] if row else None
        finally:
            out.close()
    finally:
        raw.close()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _config_files() -> list[tuple[str, Path]]:
    base = Path(settings.backup_config_dir)
    out = []
    for p in sorted(base.rglob("*")) if base.is_dir() else []:
        rel = p.relative_to(base)
        if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_CONFIG_FILE or any(x.startswith(".cert-sync") for x in rel.parts):
            continue                                   # (the last: the temporary folder of scripts/cert-sync.sh)
        out.append((f"config/{rel.as_posix()}", p))
        if len(out) >= MAX_CONFIG_FILES:
            break
    env = Path(settings.backup_env_file) if settings.backup_env_file else None
    if env and env.is_file() and not env.is_symlink() and env.stat().st_size <= MAX_CONFIG_FILE:
        out.append(("config/.env", env))               # all the settings, secrets included: it travels only in encrypted backups
    return out


def _add(tf: tarfile.TarFile, arcname: str, path: Path | None = None, data: bytes | None = None) -> None:
    info = tarfile.TarInfo(arcname)
    info.mtime, info.mode = int(datetime.now(UTC).timestamp()), 0o600
    if data is not None:
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    else:
        info.size = path.stat().st_size
        with open(path, "rb") as f:
            tf.addfile(info, f)


def _safe_name(name: str) -> bool:
    parts = name.split("/")
    return (name == "spoolsteward.db" or (parts[0] == "config" and len(parts) > 1)) and ".." not in parts and not name.startswith("/")


def create_backup(sm, folder: Path, tag_: str, *, recipient: str | None, target_name: str, instance: str,
                  now: datetime | None = None) -> Result:
    """Write and verify one backup file into `folder`. `recipient` set = encrypted, and then the secrets are included."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    name = f"{tag_}-{now:%Y%m%d-%H%M%S}.tar.gz" + (".enc" if recipient else "")
    with tempfile.TemporaryDirectory(prefix="backup-") as tmp:
        snap = Path(tmp) / "spoolsteward.db"
        schema = snapshot_db(sm, snap)
        need = snap.stat().st_size * 2 + (16 << 20)
        if shutil.disk_usage(folder).free < need:
            raise BackupError(f"Not enough free space in the destination (need about {need >> 20} MB)")
        parts: list[tuple[str, Path]] = [("spoolsteward.db", snap)]
        if recipient:
            parts += _config_files()
        manifest = {
            "format": 1, "app_version": app_version(), "created": now.isoformat(), "schema": schema,
            "instance": instance, "target": target_name, "encrypted": bool(recipient),
            "secrets_included": bool(recipient),
            "files": {arc: {"sha256": _sha256(p), "size": p.stat().st_size} for arc, p in parts},
        }
        partial = folder / (name + ".partial")
        key = None
        try:
            with open(partial, "wb") as f:
                sink = EncryptWriter(f, recipient) if recipient else f
                with tarfile.open(fileobj=sink, mode="w|gz") as tf:
                    _add(tf, MANIFEST, data=json.dumps(manifest, indent=1).encode())
                    for arc, p in parts:
                        _add(tf, arc, path=p)
                if recipient:  # after the tar is closed: it writes its tail into the sink, then the sink seals the stream
                    sink.close()
                    key = sink.key
                f.flush()
                os.fsync(f.fileno())
            verify(partial, key=key)  # re-read what is on disk: encrypted files with the key we just used
            os.chmod(partial, 0o600)
            final = folder / name
            os.replace(partial, final)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    return Result(path=final, size=final.stat().st_size)


def _walk(path: Path, passphrase: str | None, key: bytes | None, dest: Path | None) -> dict:
    """Read a backup front to back, checking every part against the manifest; extract into `dest` if given."""
    with open(path, "rb") as raw:
        encrypted = raw.read(len(MAGIC)) == MAGIC  # by content, not by name: a .partial file is checked before it is renamed
        raw.seek(0)
        src = DecryptReader(raw, passphrase, key) if encrypted else raw
        try:
            with tarfile.open(fileobj=src, mode="r|gz") as tf:
                manifest, seen = None, set()
                for member in tf:
                    fh = tf.extractfile(member)
                    if member.name == MANIFEST:
                        manifest = json.loads(fh.read(1 << 20))
                        continue
                    if manifest is None:
                        raise BackupError("Backup has no manifest first: not a backup of this gateway")
                    if not member.isfile() or not _safe_name(member.name) or member.name not in manifest["files"]:
                        raise BackupError(f"Unexpected item in the backup: {member.name}")
                    h, size = hashlib.sha256(), 0
                    if dest is not None:
                        target = dest / member.name
                        target.parent.mkdir(parents=True, exist_ok=True)
                    with (open(target, "wb") if dest is not None else nullcontext()) as out:
                        for block in iter(lambda: fh.read(1 << 20), b""):
                            h.update(block)
                            size += len(block)
                            if out is not None:
                                out.write(block)
                    want = manifest["files"][member.name]
                    if h.hexdigest() != want["sha256"] or size != want["size"]:
                        raise BackupError(f"{member.name} does not match its checksum: the backup is damaged")
                    seen.add(member.name)
        except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
            if isinstance(exc, CryptoError):
                raise
            raise BackupError(f"Backup cannot be read: {exc}") from exc
    if manifest is None or seen != set(manifest["files"]):
        raise BackupError("Backup is incomplete")
    return manifest


def verify(path: Path, passphrase: str | None = None, key: bytes | None = None) -> dict:
    return _walk(path, passphrase, key, None)


def mirror_audit(src: Path, folder: Path, instance: str, recipient: str | None) -> int:
    """Copy the compressed audit files that are not in the backup folder yet (they never change once written)."""
    if not src.is_dir():
        return 0
    dest = folder / f"audit-archive-{instance}"
    dest.mkdir(exist_ok=True)
    copied = 0
    for f in sorted(src.glob("*.jsonl.gz")):
        out = dest / (f.name + (".enc" if recipient else ""))
        if out.exists():
            continue
        partial = dest / (out.name + ".partial")
        try:
            with open(f, "rb") as r, open(partial, "wb") as w:
                sink = EncryptWriter(w, recipient) if recipient else w
                for block in iter(lambda: r.read(CHUNK), b""):
                    sink.write(block)
                if recipient:
                    sink.close()
                w.flush()
                os.fsync(w.fileno())
            os.chmod(partial, 0o600)
            os.replace(partial, out)
            copied += 1
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    return copied


def list_backups(folder: Path, instance: str, target_id: int) -> list[dict]:
    out = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return out
    for p in entries:
        m = NAME_RE.match(p.name)
        if m and m["inst"] == instance and int(m["tid"]) == target_id and p.is_file():
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "encrypted": bool(m["enc"]),
                        "ts": datetime.strptime(m["ts"], "%Y%m%d-%H%M%S").replace(tzinfo=UTC)})
    return sorted(out, key=lambda b: b["ts"], reverse=True)


def keep_set(stamps: list[datetime], daily: int, weekly: int, monthly: int, tz: str) -> set[int]:
    """Indexes (stamps newest first) to keep: the newest of each of the last `daily` days, `weekly` weeks and
    `monthly` months that have a backup. The newest backup is always kept."""
    zone = ZoneInfo(tz)
    keep: set[int] = {0} if stamps else set()
    for limit, key in ((daily, lambda d: d.date()), (weekly, lambda d: d.isocalendar()[:2]),
                       (monthly, lambda d: (d.year, d.month))):
        seen: list = []
        for i, s in enumerate(stamps):
            k = key(s.astimezone(zone))
            if k not in seen:
                seen.append(k)
                if len(seen) <= limit:
                    keep.add(i)
    return keep


def prune(folder: Path, instance: str, target_id: int, daily: int, weekly: int, monthly: int) -> list[str]:
    """Delete the backups of this target that no retention rule keeps. Only files with our own name pattern."""
    items = list_backups(folder, instance, target_id)
    keep = keep_set([b["ts"] for b in items], max(daily, 1), max(weekly, 0), max(monthly, 0), settings.timezone)
    removed = []
    for i, b in enumerate(items):
        if i not in keep:
            (folder / b["name"]).unlink(missing_ok=True)
            removed.append(b["name"])
    return removed


def restore_audit(mirror: Path, dest: Path, passphrase: str | None) -> int:
    """Put the mirrored audit files back (decrypting them) where the gateway looks for its archive."""
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in sorted(mirror.glob("*.jsonl.gz*")):
        if f.suffix == ".partial":
            continue
        name = f.name.removesuffix(".enc")
        out = dest / name
        if out.exists():
            continue
        partial = dest / (name + ".pending-restore")
        try:
            with open(f, "rb") as r, open(partial, "wb") as w:
                src = DecryptReader(r, passphrase) if f.name.endswith(".enc") else r
                for block in iter(lambda: src.read(1 << 20), b""):
                    w.write(block)
            os.replace(partial, out)
        except BaseException:
            partial.unlink(missing_ok=True)  # a wrong passphrase must not leave half a file behind
            raise
        count += 1
    return count


def restore(path: Path, passphrase: str | None, *, db_path: Path, config_out: Path, audit_dir: Path) -> dict:
    """Replace the database with the one in the backup (the old file is kept next to it), bring back the audit
    files mirrored beside the backup, and leave the configuration files in `config_out` for the operator to
    place (the gateway cannot overwrite its own read-only configuration)."""
    with tempfile.TemporaryDirectory(prefix="restore-", dir=db_path.parent) as tmp:
        manifest = _walk(path, passphrase, None, Path(tmp))
        rev = manifest.get("schema")
        if rev and rev not in known_revisions():
            raise BackupError(f"This backup comes from a newer version (schema {rev}): update the gateway first")
        if not (Path(tmp) / "spoolsteward.db").is_file():  # checked before anything is moved: a backup without a database changes nothing
            raise BackupError("This backup holds no database (spoolsteward.db): nothing was restored")
        if db_path.exists():
            keep = db_path.with_name(f"{db_path.name}.before-restore-{datetime.now():%Y%m%d-%H%M%S}")
            shutil.move(db_path, keep)
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        shutil.move(str(Path(tmp) / "spoolsteward.db"), db_path)
        os.chmod(db_path, 0o600)
        has_config = (Path(tmp) / "config").is_dir()
        if has_config:
            shutil.copytree(Path(tmp) / "config", config_out, dirs_exist_ok=True)
    mirror = path.parent / f"audit-archive-{manifest['instance']}"
    audit = restore_audit(mirror, audit_dir, passphrase) if mirror.is_dir() else 0
    return {"manifest": manifest, "audit_files": audit, "config": has_config}
