# Backup and restore

[Documentation index](../README.md) · English · [Italiano](../it/backup.md)

There are two independent mechanisms:

| | Built-in backups (portal) | `scripts/backup.sh` (host side) |
|---|---|---|
| Who | each administrator, plus one *general* | the person running the server |
| What | database (consistent copy while people print) and audit archive; with encryption also `.env` (all the settings and secrets) and the TLS certificate | database, `config/` (with secrets), `.env`, audit archive and CUPS queues |
| When | scheduled, once a day | by hand |
| Use for | everyday protection | **moving the machine** |

## 1. Built-in backups (menu *Backup*)

Every administrator creates **their own** targets, with folder, hour, number of copies to keep (last N daily, weekly, monthly)
and encryption. They edit and delete only their own; *Run now* is available to all. The *General* backup of the installation is
shown read-only: it is set in `.env` (`SPOOLSTEWARD_BACKUP_*`).

![A backup target: owner, folder, time, copies kept, encryption with a passphrase, the last result and the buttons Run now, Edit, Suspend and Delete](../images/en/admin-backup.png)

- A backup is written to `.partial`, **read back and verified**, renamed; only then are older copies removed.
  *Run now* adds a copy without deleting any (the next scheduled run prunes).
- If a backup failed, or the last one is older than 48 h, the page shows it in red **and a notice appears in the bell** with the name of the backup (see "Notices when a backup fails" below). The notice closes by itself at the first successful backup.
- Retention affects only files carrying the name of the same gateway and target.
- Each file is `spoolsteward-<instance>-t<target>-<UTC time>.tar.gz` (plus `.enc` if encrypted) with a `MANIFEST.json`
  (sha256 and size of each part, app version, schema revision).
- Without encryption the file **contains no secrets** (TLS key, `SECRET_KEY`, RADIUS secret).

### Where

Only inside `data/backups` (`BACKUP_DIR`, mounted at `/backups`); an administrator picks a sub-folder (letters, digits, space, `. _ -` and
`/`, max 4 levels) and can not leave it (`..`, absolute paths and symlinks are refused). For a NAS or a USB disk mount it on the
server **inside that folder before starting the gateway** (a mount done later needs a gateway restart, see below).

### Saving to a network disk (NAS, SMB/CIFS, NFS) or a USB disk

1. **Mount it on the server, inside `data/backups`** (the folder `BACKUP_DIR` points to). Use `/etc/fstab` so it comes back after a reboot, for example:

   ```
   # SMB/CIFS: uid/gid = the PUID/PGID of .env, so the gateway can write. Keep the password in a root-only file.
   //nas.lan/backups  /srv/spoolsteward/data/backups/nas  cifs  credentials=/root/.nas-cred,uid=1000,gid=1000,vers=3.0,_netdev,nofail,x-systemd.automount  0 0
   # NFS: the exported folder must be writable by that user id (check "root squash" on the NAS)
   nas.lan:/export/backups  /srv/spoolsteward/data/backups/nas  nfs  _netdev,nofail,x-systemd.automount,soft,timeo=100  0 0
   ```
2. Mount it **before** starting the gateway. A mount made later is not seen by a container that is already running: restart it
   (`docker compose restart gateway`).
3. In the backup form type the sub-folder (`nas/...`) and leave **"The folder is a network disk or USB disk: write only if it is mounted"** ticked
   (it is ticked by default for new backups), and save *while the disk is mounted*. The gateway puts a small file `.spoolsteward-destination` in it and, from then on, **refuses to back up
   when the file is not there**. Without this a disk that fails to mount leaves an empty folder on the server's own disk, and the
   backups silently fill it (and are not outside the machine at all). The general backup (the one of `.env`) has the option off by default (`SPOOLSTEWARD_BACKUP_REQUIRE_MARKER=false`): its folder always exists inside the
   container, even with the disk unmounted. If you point it at a network disk, set `true` and create the file by hand once, with the disk mounted:
   `touch data/backups/nas/<folder>/.spoolsteward-destination`.

Behaviour of the gateway with a network disk:

- A disk that does not answer: the *Backup* page loads in about 6 seconds and shows the problem; a backup fails after 20 seconds
  with "The backup disk did not answer…". At most 4 requests wait on a dead disk at the same time; the rest of the portal is not affected.
- With the marker option on, an unmounted disk never receives a file; the backup fails with "The marker file is missing…".
- A disk mounted after the gateway started is not seen by default. Restart the gateway, or bind the folder with
  `bind: {propagation: rslave}` in `docker-compose.yml` (the folder must be on a shared mount, which is the default with systemd).
- On SMB/CIFS and some NFS setups file permissions are decided by the mount options, **not** by the gateway: the usual `chmod 600` has
  no effect and anyone who can read the share can read the files. **Use encryption** for backups on a share.
- Use `soft` mounts (NFS: `soft,timeo=100`; CIFS: `soft`), never `hard`, so a dead NAS does not freeze the server.
- A disk that stops answering **while a backup is being written** can leave that backup stuck: the next ones then say "Another backup is running" until the gateway is restarted (`docker compose restart gateway`). Nothing already saved is lost.
- The backup is only "outside the machine" if the share is on another machine. A USB disk plugged into the server protects from a broken disk, not from a fire.

### Notices when a backup fails

When a backup fails, a notice with the **real name** of the backup appears in the administrators' bell. Example, with the disk unmounted:

> **Backup not done: the destination folder can not be reached.** The latest backup of "Office NAS" was not saved, because the network disk or USB disk does not seem to be connected. Nothing was written. Check that it is switched on and mounted, then press "Run now". Last successful backup: 2026-10-12 03:00:04.

For any other error (disk full, permissions, passphrase…) the notice says "Backup failed" and the reason. The gateway retries every hour and the notice closes by itself when a
backup succeeds. If you do not want to wait, press *Run now* after remounting the disk.

### Encryption

- **Passphrase** (recommended; **not** the account password — the gateway does not know it, it changes, and for RADIUS it is not the user's),
- **recovery key** created on the server (`spoolsteward-admin backup-key`: the passphrase never goes through the portal),
- or **none**.

How it works: a key pair is made once. The public half encrypts each file (ephemeral X25519 + HKDF + AES-256-GCM in 1 MiB blocks, last
block flagged); the private half is kept **wrapped** by scrypt of the passphrase and copied into the file header, so
*file + passphrase* are enough. The gateway never stores the passphrase: **without it the copies can not be opened**, not even by
another administrator. The passphrase is never written to the audit. A new encrypted file is re-read with the key just used.

### Save the secret key

`SPOOLSTEWARD_SECRET_KEY` in `.env` encrypts the file names kept in the audit ([data-protection.md](data-protection.md)). A backup
**without encryption does not contain it**, on purpose (it holds no secrets). So if the server breaks, you restore an
unencrypted backup on a new machine and `scripts/docker-init.sh` creates a **new** key, the database comes back whole (users,
quotas, pages, dates, printers) but the file names stay `[encrypted]` for good.

- Copy the `SPOOLSTEWARD_SECRET_KEY=...` line of `.env` into a password manager or another safe place, **once**, after installing.
- Or make encrypted backups: they include `.env`, so the key travels with the database.
- When you move to another machine, copy `.env` too (`scripts/backup.sh` does).

The gateway helps: after the first start the first-run administrator (`SPOOLSTEWARD_BOOTSTRAP_ADMIN_USERNAME`) finds a reminder in their
own bell, once (if that account does not exist, every administrator sees it). If the gateway later starts with a different key, the
bell says so and tells how many names can no longer be read; putting the old key back in `.env` and restarting makes them readable
again.

## 2. Restore

```sh
docker compose stop gateway mdns
docker compose run --rm -it --no-deps gateway spoolsteward-admin backup-restore /backups/<folder>/<file>
docker compose up -d
```

The restore reads and checks the whole file first; a damaged or truncated file, a wrong passphrase, a backup that holds no database or one made by a newer version
is refused and the current database is not touched. Otherwise the previous database stays beside as `spoolsteward.db.before-restore-…`; configuration files go to `/data/restored-config` (copy them
into `./config` on a new machine). `backup-verify <file>` checks a file without restoring; `backup-run [--id N]` runs backups by hand.
Restore is the way back after an upgrade you want to undo.

## 3. Host-side full backup

```sh
scripts/backup.sh backup [DEST_DIR]        # writes DEST_DIR/spoolsteward-v<version>-YYYYMMDD-HHMMSS.tar.gz  (default ./backups; <version> = the version running)
scripts/backup.sh restore FILE.tar.gz      # stops the gateway first and asks for confirmation
```

Includes the database (SQLite backup call), `config/` (with secrets), `.env`, the audit archive and the CUPS configuration (queues).
Not included: images and the spool of jobs in progress. The archive holds secrets: keep it as private as the server.
`restore` brings back the `.env` of the backup, hence its version too: if the files in the folder are of another version, it tells you to run `scripts/update.sh <version>` (see "Going back to an older version" in [installation.md](installation.md)).
`scripts/backup.sh` needs no Internet: it uses the image the stack already runs, and a restore checks everything first and stops the application only when it is sure it can go on.
`restore` has only been tested by hand.

## 4. Limits

SQLite only (with another database use its own dump tool); the built-in backups do not include CUPS queues (recreate them from
`CUPS_QUEUES`, or use `scripts/backup.sh`); configuration files are not restored automatically (the container has them read-only);
no direct remote destination (SSH/S3): mount it on the server; the passphrase is not recoverable.
