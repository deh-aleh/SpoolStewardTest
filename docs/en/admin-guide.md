# Administrator guide

[Documentation index](../README.md) · English · [Italiano](../it/guida-amministratore.md)

For people who run the installation. The portal is at `https://<server>:8631/` (sign-in at `/login`, first-run setup at `/setup`). The top menu:

| Menu | Page | What you do there |
|---|---|---|
| **Audit** | `/admin/jobs` | Who printed or scanned what; filters, CSV, usage report (`/admin/report`), cancel queued jobs → [audit.md](audit.md) |
| **Users** | `/admin/users` | Create local or RADIUS users, edit, reset password, revoke keys, delete |
| **Groups** | `/admin/groups` | Create, rename, limits, "requestable", delete |
| **Requests** | `/admin/requests` | Approve, edit or reject quota, group and permission requests (badge = pending); active grants, give or revoke extra quota or a permission directly, history → [quotas.md](quotas.md) |
| **Actions** | `/admin/actions` | Who did what in the portal |
| **Printers** | `/admin/printers` | State, ink, counters; block, hide, rename, move up/down (your own order), test printers, maintenance print, test print → [printers.md](printers.md) |
| **Backup** | `/admin/backups` | Your scheduled backups → [backup.md](backup.md) |
| **Archive** | `/admin/archive` | Compressed old audit, readable and downloadable |
| **Print / Scan / Profile** | `/me…` | The same pages users have → [user-guide.md](user-guide.md) |
| **Logo, name and (user)** | `/me/settings` | The block at the top left opens the settings: theme, language, refresh |
| **Bell** (administrators only) | `/admin/notices` | Things the gateway noticed (badge = open ones), see "Notices" below |
| **?** (everybody) | `/docs` | This documentation, in English or Italian (switch at the top of each page). Users who are not administrators see only the user guide and the Windows guide |
| **Maintenance** (red traffic cone) | — | Stops print and scan for everyone but administrators |
| **Door with arrow** | — | Log out |

## 1. First steps

1. Set the `administrator` password with the one-time code ([installation.md](installation.md#36-first-administrator)).
2. Create your **own** administrator account (Users → create → administrator) and use it day to day. `administrator` is the emergency account: it belongs to no group and nothing limits it (no quota, no permission rule), so keep it for recovery.
3. Edit the **default** group limits (Groups) and create groups for the people who need different limits.
4. Create users, or let RADIUS users log in. Tell people to create a print key ([user-guide.md](user-guide.md)).
5. Set up a backup ([backup.md](backup.md)).

## 2. Users

The list is read-only: a user's own limit or the inherited one (italics), active grants as a coloured `+N`, consumption and active keys.
**Edit** opens the page with every field, which shows the **source** of the account (local or RADIUS, for information only), and **Delete** (type the name to confirm).

- Create **local**: a temporary password is shown once; the user changes it at first login.
- Create **RADIUS**: choose the group; the person signs in with their office password.
- **Reset password** (local): new temporary password, shown once.
- **Keys**: the list per user is at `/admin/users/<id>/keys`. For each key you can **switch it off** (its expiry keeps running; switch it on again later), **extend it** (the new expiry counts from today, the time left is not added) or **revoke it**; "Revoke all" stops every device of the user. The user gets a notice for each action (not for their own). Revoked and expired keys disappear after `SPOOLSTEWARD_KEY_HISTORY_DAYS` days (14).
- **Admin flag**: promotion moves the user to the `administrator` group, demotion to `default`.
- Protected: you can not disable, demote or delete yourself, the emergency account or the last administrator.

Deleting keeps the history ([accounts.md](accounts.md#7-deleting-a-user)).

## 3. Groups and limits

A group has limits (jobs / pages, weekly / monthly) and a **requestable** flag. System groups `default` and `administrator` can not be
deleted; deleting another group moves its users to `default`. Personal limits are set in the user's edit page. Details in [quotas.md](quotas.md).

**Advanced permissions.** At the bottom of the group page and of the user page, the **Advanced permissions** button opens colour, one-sided
prints, which printers, and which days and hours. A user's field is "as the group" or has its own value. The emergency administrator has no group and no
limits at all; its page shows the block switched off. `SPOOLSTEWARD_PERMISSION_MODE` decides whether a client that explicitly asks for colour or
a one-sided print that is not allowed is corrected (`adjust`, default) or refused (`refuse`). Read the notes in
[quotas.md](quotas.md#5-advanced-permissions) before choosing: with `refuse` many Windows and Android jobs are refused. A user is told in
their bell when you change their own permissions, and when a job of theirs is refused or corrected.

## 4. Requests and grants

Everything is on one page, *Requests*, in three parts:

- **Pending requests**: edit the amount or the kind before approving, add one note (the user sees it, whether you approve or reject). A permission request (colour, one-sided, a printer, outside the hours) is approved *permanent* or *until the end of* a date you choose. If two administrators approve at once, or someone double-clicks, it is granted once.
- **Active grants**: the extra quota and the permissions that count now; revoke them, or give one directly with a note (choose *quota* or *a permission* in the form).
- **History** (closed until you open it): the latest decisions and the grants that no longer count (ended or revoked).
  *Delete ended grants* removes those from the list; active grants are not touched, and the administrators' log (*Actions*) keeps
  who granted what and who deleted them. It removes the ended permission grants too.
  More in [quotas.md](quotas.md#asking-for-a-permission).

Everything is logged.

## 5. Printers

Block a printer (nobody prints, administrators included), hide it from mDNS, create test printers, set up the periodic maintenance
print, send a test print with statistics, switch on **gateway maintenance** (red traffic-cone button). See [printers.md](printers.md).

## 6. Audit and archive

Use filters and the CSV export; cancel queued jobs; read archived files; verify their integrity. Nothing can be deleted
([audit.md](audit.md)).

## 7. Command line (`spoolsteward-admin`)

Inside the container: `docker compose exec gateway spoolsteward-admin <command>` (add `-it` when it asks for a password).

| Command | Purpose |
|---|---|
| `set-admin-password [user]` | create or update a **local** administrator (default `administrator`) and set its password |
| `make-admin <user>` | grant admin rights (creates the user if needed) |
| `set-group <user> <group>` | move a user to a group (creates the group) |
| `group-limit <group> <limit\|none> [--period weekly\|monthly] [--metric jobs\|pages]` | set a group limit; `none` removes it |
| `user-limit <user> <limit\|none> [--period …] [--metric …]` | set a personal limit |
| `jobs [-n N]` | show the latest audit rows (and warn about archives) |
| `audit-archive [--force]` | archive old audit rows now |
| `backup-key` | create a backup recovery key from a passphrase |
| `backup-run [--id N]` | run the enabled backups now |
| `backup-verify <file>` | check a backup file |
| `backup-restore <file>` | restore database and audit files (stop the gateway first) |

## 8. Notices (the bell)

The bell in the top bar (administrators only) lists what the gateway noticed and nobody asked about. Each notice shows the
last time, the source, and how many times it happened; *Dismiss* closes it (the dismissal is recorded in *Actions*).
The kinds of notice:

| Notice (as shown in the bell) | Meaning | Closes by itself when |
|---|---|---|
| A scan request without a user name and password from IP was refused | `SPOOLSTEWARD_ESCL_ANONYMOUS` is off and a client sent no credentials ([scanning.md](scanning.md)) | never (dismiss it) |
| **Backup not done: the destination folder can not be reached** | The marker file of the destination is missing (disk not mounted); nothing was written ([backup.md](backup.md)) | the next backup works |
| **Backup failed** | Any other error, with the reason; both notices carry the real name of the backup | the next backup works |
| **The TLS certificate has expired** | Clients no longer trust the gateway ([certificates.md](certificates.md)) | a valid certificate is in place and the gateway restarted |
| **The certificate could not be updated** | The last run of `scripts/cert-sync.sh` failed | the next run works |
| **The announced address is not in the TLS certificate** | Clients that discover the printers through mDNS get a certificate error | the SAN lists the address |
| **The TLS certificate expires on DATE** | Only if `SPOOLSTEWARD_CERT_EXPIRY_WARN_ENABLED=true` | the certificate is renewed |
| **Paper jam** / **Out of paper** | A physical printer reports it (checked once a minute, reported after two consecutive checks) | the printer stops reporting it |
| **Ink or toner is out** / **running low** | Out: level 0, or the printer's state reason says so. Low: the printer's own low mark. Both name the supplies | the printer stops reporting it |
| **Save the secret key in a safe place** | Once, after the first start: copy `SPOOLSTEWARD_SECRET_KEY` from `.env`. Goes to the first-run administrator (`SPOOLSTEWARD_BOOTSTRAP_ADMIN_USERNAME`) in their own bell, or to all administrators if that account does not exist or `SPOOLSTEWARD_BOOTSTRAP_ADMIN=false`. Not repeated at a restart; repeated once if the key changes | never (dismiss it) |
| **The secret key has changed** | `SPOOLSTEWARD_SECRET_KEY` differs from the previous start; says how many file names can no longer be read | never (dismiss it) |
| **No secret key is set** | `SPOOLSTEWARD_SECRET_KEY` is `none` or missing: file names are not encrypted and sessions end at every restart | a key is set |

**Printer problems and users.** The notices above about printers are always for administrators. With `SPOOLSTEWARD_PRINTER_ALERTS_USERS=true` (default) the people who have a
print waiting on that printer get a notice in their own bell too (jam, paper and ink out; not "running low"), once per problem and only them, not everybody.
It closes by itself when the printer is fine again. With `false` only the administrators are told.

**One bell.** An administrator has a single bell: `/admin/notices` shows first the notices about their own account ("For you": their keys, group, quota,
requests) and then the gateway's ones, and the red number adds both. `/me/notices` sends administrators there.

To keep one client that retries 100 times from filling the list, the same notice from the same source is written again only
after an interval; the repeats in between are counted. The interval depends on `SPOOLSTEWARD_LOG_LEVEL`, and the same text is
written to the container log with the same limit:

| `SPOOLSTEWARD_LOG_LEVEL` | Same notice, same source, reported again after |
|---|---|
| `DEBUG` | 1 minute |
| `INFO` (default) | 1 day |
| `WARNING` | 3 days |
| `ERROR` | 7 days |

`SPOOLSTEWARD_NOTICE_INTERVAL` (seconds) replaces the table. At most 200 open notices of a kind are kept; dismissed ones are removed after 90 days.

## 9. Operations

- Health: `GET /healthz` → 200 or 503, body `{"status":"ok"}`. It needs no login. `SPOOLSTEWARD_HEALTHZ_INFO=version,printers` adds the version and the number of queues (usb / network / virtual / total); the default is the status only. Logs: `docker compose logs gateway`.
- Migrations run automatically at start. Back up before updating an important database.
- File names in the audit: the green/red button at the top right of the *Audit* page stops saving them, `.env` can erase them after some days, and they are stored encrypted ([data-protection.md](data-protection.md)). Do not change `SPOOLSTEWARD_SECRET_KEY`.
- Updates: a note appears at the top of the portal when a newer release exists. Update with `scripts/update.sh`, or with the *Update* button in the menu *Backup* when the optional updater container runs and a scheduled backup works ([installation.md](installation.md#5-updates)). The updater controls Docker, which is full control of the server: keep it off if you do not need it.
- Do not leave test accounts or test printers on a real installation.
- Protect `config/`, `.env`, `data/` and the volume: they hold secrets and personal data.
