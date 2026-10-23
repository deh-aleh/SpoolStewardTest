# Data protection: file names, encryption and retention

[Documentation index](../README.md) · English · [Italiano](../it/protezione-dati.md)

The audit ([audit.md](audit.md)) records who printed and scanned what. The **file name** is personal data, so it is encrypted at rest; this page
lists what is and is not protected and the settings that reduce what the server keeps.

## 1. What is protected and what is not

| Data | How it is kept |
|---|---|
| **Name of the file** printed or scanned | **Encrypted** in the database, in the audit archive (the `.jsonl.gz` files) and therefore also in backups |
| Passwords of local accounts | Only the fingerprint (argon2 hash): the password cannot be recovered |
| Print keys | Only the fingerprint (sha256) |
| User name, groups, printer names, the administrators' action log, reasons of requests (quota, group, permission) and notes of grants, permissions and allowed hours, IP addresses | **In clear** in the database |
| Amounts (pages, date, state of each print) | In clear: quotas need them |
| The database as a file | Not encrypted: whoever copies the file reads everything except the file names (stored as `enc1:<base64>`, AES-256-GCM) |
| Backups | Encrypted only if you choose encryption in the *Backup* menu (recommended) |

## 2. The "File names" button (green / red)

In *Administration → Audit* (`/admin/jobs`), top right, there is a button with a dot:

- **green dot — "File names are being saved"**: every print and scan records the file name (the normal behaviour);
- **red dot — "File names are not being saved"**: new prints and scans appear in the audit **without the file name**. Who, when, how many pages, on which printer and how it went are still kept, so quotas work as before.

Clicking to turn it off shows a confirmation question. The names **already saved stay**; new ones are no longer written. Click again to go back to saving them.
The choice is recorded in the administrators' action log.

## 3. Forgetting old names: retention

If you want names to disappear after a while, turn on retention in `.env` (then `docker compose up -d`):

| Setting | What | Default |
|---|---|---|
| `SPOOLSTEWARD_JOB_TITLE_RETENTION_ENABLED` | `true` turns on the erasing of old names | `false`: **no retention**, names stay |
| `SPOOLSTEWARD_JOB_TITLE_RETENTION_DAYS` | After how many days the name is erased | `90` |

Once a day the gateway erases the name from the rows older than the chosen number of days. **The row stays** (who, when, pages, state): quotas and counters need it.

**What is not touched.** The audit archive files already written are immutable (so it can be proved they were not changed) and are left as they were: the names they
contain stay, encrypted. From the moment you turn retention on, the new archive files no longer contain the names of rows past the limit.

## 4. The key: do not change `SPOOLSTEWARD_SECRET_KEY`

Names are encrypted with a key derived from `SPOOLSTEWARD_SECRET_KEY` (in `.env`, created by `scripts/docker-init.sh`).

> **If you change or lose `SPOOLSTEWARD_SECRET_KEY`, the file names already saved can no longer be read**: the portal shows `[encrypted]` in their place. Everything else keeps working
> (sessions are reset and everybody logs in again).

- **Encrypted** backups include `.env`, so the key too; unencrypted ones do not (and without the key the names they contain cannot be read: that is intended).
- If the key is `none` the gateway encrypts nothing (and warns in the log and in the bell), because a different key at every start would make names unreadable after the next restart.
- **Keep a copy of the key outside the server** (a password manager): after the first start the first-run administrator finds a reminder in the bell, once. If the gateway starts with a key that is not the one of the previous start, it says so in the bell with the number of names that can no longer be read ([backup.md](backup.md#save-the-secret-key)).
- If you had names in clear before, at the first start with the key the gateway encrypts them by itself, in chunks, without stopping.

**Searching by file name** in the audit looks in the latest 5,000 actions that match the other filters (names are encrypted: to search, they are opened one by one). Searching by user has no such limit.

## 5. Encrypting the disk (the main measure)

Encrypting the file names protects a database that is copied alone. To protect **everything** (users, groups, audit, keys, certificate) the right way is to encrypt the server's disk:
if someone takes the computer away, without the unlock password they see only meaningless data. The operating system does it, not SpoolSteward (inside a container it cannot).

**The simplest way: encrypt the whole disk when you install Linux.** The Ubuntu and Debian installers have a box *"Encrypt the new installation"* (it uses LUKS, the Linux standard).
You choose a password. Remember that at every power-on the server asks for it: a server with no keyboard or screen needs a way to enter it.

**If the server is already installed and you cannot reinstall:** the encryption must be done on an empty disk or partition, with `cryptsetup`. Warning: `luksFormat` **erases everything** on the
disk you name. There are three places to put on the encrypted disk:

| What | Where it is | Why |
|---|---|---|
| The database | in the Docker volume `portal-data`, inside Docker's data folder (usually `/var/lib/docker`) | it holds users, groups, quotas, audit |
| `data/` | in the SpoolSteward folder | audit archive and backups |
| `config/` and `.env` | in the SpoolSteward folder | TLS key and secrets |

Moving Docker's data folder to the encrypted disk (`data-root` in `/etc/docker/daemon.json`) and making Docker start only after unlocking takes some Linux practice:
if you have never done it, prefer reinstalling with an encrypted disk.

**Backups.** Always encrypt them (passphrase or recovery key, *Backup* menu): an unencrypted backup contains the whole database, and often ends up on an unprotected network disk.

## 6. One last thing: the CUPS history

Besides SpoolSteward's audit, **CUPS** (the printing program behind it) keeps the history of the latest jobs (up to 2,000, setting `MaxJobs`),
with the document name, in the Docker volume `spoolsteward_cups-spool`. It does not keep the content of documents (`PreserveJobFiles No`), but it keeps the names.
The button and the retention of this page do **not** touch that history: the oldest names disappear by themselves when jobs go over the limit. If you need to
erase them right away, stop the stack, delete that volume and start again:

```sh
docker compose down
docker volume rm spoolsteward_cups-spool
docker compose up -d
```

The printer queues are not lost (they are in the volume `cups-config`). If you have several installations or gave the compose project another name, the volume name changes: check it with `docker volume ls`.
