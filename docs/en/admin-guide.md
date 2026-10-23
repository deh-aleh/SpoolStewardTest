# Administrator guide

[Documentation index](../README.md) · English · [Italiano](../it/guida-amministratore.md)

For people who run the installation. The portal is at `https://<server>:8631/` (sign-in at `/login`, first-run setup at `/setup`). The top menu:

| Menu | Page | What you do there |
|---|---|---|
| **Audit** | `/admin/jobs` | Who printed or scanned what; filters, CSV, usage report (`/admin/report`), cancel queued jobs → [audit.md](audit.md) |
| **Users** | `/admin/users` | Create local or RADIUS users, edit, reset password, revoke keys, delete |
| **Groups** | `/admin/groups` | Create, rename, limits, "requestable", delete |
| **Requests** | `/admin/requests` | Approve, edit or reject quota, group and permission requests (badge = pending); active grants, direct grants, history → [quotas.md](quotas.md) |
| **Actions** | `/admin/actions` | Who did what in the portal |
| **Printers** | `/admin/printers` | State, ink, counters; block, hide, rename, order, test printers, maintenance print, test print → [printers.md](printers.md) |
| **Backup** | `/admin/backups` | Your scheduled backups → [backup.md](backup.md) |
| **Archive** | `/admin/archive` | Compressed old audit, readable and downloadable |
| **Print / Scan / Profile** | `/me…` | The same pages users have → [user-guide.md](user-guide.md) |
| **Logo, name and (user)** | `/me/settings` | Theme, language, refresh |
| **Bell** (administrators) | `/admin/notices` | Things the gateway noticed (badge = open ones), see §8 |
| **?** (everybody) | `/docs` | This documentation, in English or Italian. Users who are not administrators see only the user guide and the Windows guide |
| **Maintenance** (red traffic cone) | — | Stops print and scan for everyone but administrators |
| **Door with arrow** | — | Log out |

## 1. First steps

1. Set the `administrator` password with the one-time code ([installation.md](installation.md#36-first-administrator)).
2. Create your **own** administrator account (Users → create → administrator) and use it day to day. `administrator` is the emergency account: no group, no quota, no permission rule; keep it for recovery.
3. Edit the **default** group limits (Groups) and create groups for people who need different ones.
4. Create users, or let RADIUS users log in. Tell people to create a print key ([user-guide.md](user-guide.md)).
5. Set up a backup ([backup.md](backup.md)).

## 2. Users

The list is read-only: a user's own limit or the inherited one (italics), active grants as a coloured `+N`, consumption and active keys. **Edit** opens the page with every field and the account's **source** (local or RADIUS, informational); **Delete** asks you to type the name.

![The Users page: the administrator and a local user who must change the password, with group, limits, weekly usage and keys](../images/en/admin-users.png)

- **Local**: a temporary password is shown once; the user changes it at first login. **Reset password** gives a new one. **RADIUS**: choose the group; the person signs in with their office password.
- **Keys** (`/admin/users/<id>/keys`): **switch off** (the expiry keeps running), **extend** (counted from today) or **revoke** each key; "Revoke all" stops every device of the user. The user gets a notice for each action (not for their own). Revoked and expired keys disappear after `SPOOLSTEWARD_KEY_HISTORY_DAYS`.
- **Admin flag**: promotion moves the user to the `administrator` group, demotion to `default`.
- You can not disable, demote or delete yourself, the emergency account or the last administrator. Deleting keeps the history ([accounts.md](accounts.md#7-deleting-a-user)).

## 3. Groups and limits

A group has limits (jobs / pages, weekly / monthly) and a **requestable** flag. The system groups `default` and `administrator` can not be deleted; deleting another group moves its users to `default`. Personal limits are in the user's edit page. Details: [quotas.md](quotas.md).

![The Groups page: the two system groups without limits and a group with 10 jobs and 20 pages a week that users may ask to join](../images/en/admin-groups.png)

**Advanced permissions** (colour, one-sided, printers, days and hours) are at the bottom of the group and user pages. `SPOOLSTEWARD_PERMISSION_MODE` decides whether a client that explicitly asks for something not allowed is corrected (`adjust`, default) or refused (`refuse`): read
[quotas.md](quotas.md#5-advanced-permissions) first. A user is told in their bell when you change their own permissions and when a job of theirs is refused or corrected.

## 4. Requests and grants

One page, *Requests*, in three parts:

- **Pending requests**: edit the amount or the kind before approving, add one note (the user sees it). A permission request is approved *permanent* or *until the end of* a date. Two simultaneous approvals grant once.
- **Active grants**: extra quota and permissions that count now; revoke them, or give one directly with a note (choose *quota* or *a permission*).
- **History** (closed until opened): latest decisions and grants that no longer count. *Delete ended grants* removes those (both kinds); the administrators' log keeps who granted what.

More in [quotas.md](quotas.md#asking-for-a-permission). Everything is logged.

## 5. Printers, audit and archive

Block or hide a printer, create test printers, set up the maintenance print, send a test print, switch on **gateway maintenance**: [printers.md](printers.md). For the audit, filters, CSV, cancelling queued jobs and reading archived files: [audit.md](audit.md). Nothing can be deleted there.

## 6. Command line (`spoolsteward-admin`)

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

## 7. Operations

- Health: `GET /healthz` → 200 or 503, body `{"status":"ok"}`, no login. `SPOOLSTEWARD_HEALTHZ_INFO=version,printers` adds more. Logs: `docker compose logs gateway`.
- Migrations run automatically at start. Back up before updating an important database.
- File names in the audit: the green/red button at the top right of *Audit* stops saving them; `.env` can erase them after some days or encrypt them ([data-protection.md](data-protection.md)).
- Updates: a note appears at the top when a newer release exists. Update with `scripts/update.sh`, or with the *Update* button in *Backup* when the optional updater container runs and a scheduled backup works ([installation.md](installation.md#5-updates)). The updater controls Docker, which is full control of the server: keep it off if you do not need it.
- Do not leave test accounts or test printers on a real installation. Protect `config/`, `.env`, `data/` and the volume: they hold secrets and personal data.

## 8. Notices (the bell)

The bell lists what the gateway noticed and nobody asked about, with the last time, the source and how many times it happened; *Dismiss* closes it (recorded in *Actions*).

| Notice | Meaning | Closes by itself when |
|---|---|---|
| A scan request without a user name and password from IP was refused | `SPOOLSTEWARD_ESCL_ANONYMOUS` is off and a client sent no credentials ([scanning.md](scanning.md)) | never (dismiss it) |
| **Backup not done: the destination folder can not be reached** | The marker file is missing (disk not mounted); nothing was written ([backup.md](backup.md)) | the next backup works |
| **Backup failed** | Any other error, with the reason; both carry the backup's real name | the next backup works |
| **The TLS certificate has expired**, **could not be updated**, **the announced address is not in the certificate**, **expires on DATE** | [certificates.md](certificates.md) | the cause is gone |
| **Paper jam** / **Out of paper** | A physical printer reports it (checked each minute, reported after two checks) | the printer stops reporting it |
| **Ink or toner is out** / **running low** | Out: level 0 or the printer says so. Low: the printer's own mark. Both name the supplies | the printer stops reporting it |
| **Save the secret key in a safe place** | Once, to the first-run administrator (or to all administrators if that account does not exist); repeated once if the key changes ([backup.md](backup.md#save-the-secret-key)) | never (dismiss it) |
| **The secret key has changed** | The key differs from the previous start; says how many encrypted file names can no longer be read | never (dismiss it) |
| **No secret key is set** | `SPOOLSTEWARD_SECRET_KEY` is `none` or missing: sessions end at every restart and the printers are not announced (mDNS) | a key is set |

**Printer problems and users.** Printer notices are always for administrators. With `SPOOLSTEWARD_PRINTER_ALERTS_USERS=true` (default) the people with a print waiting on that printer also get one (jam, paper and ink out; not "running low"), and only them. It closes when the printer is fine again.

**One bell.** An administrator has a single bell: `/admin/notices` shows first the notices about their own account ("For you": keys, group, quota, requests) and then the gateway's; the red number adds both. `/me/notices` sends administrators there.

So that one client retrying 100 times does not fill the list, the same notice from the same source is written again only after an interval, and the repeats are counted; the same text goes to the container log with the same limit. The interval depends on `SPOOLSTEWARD_LOG_LEVEL`
(`DEBUG` 1 minute, `INFO` 1 day, `WARNING` 3 days, `ERROR` 7 days) or is set by `SPOOLSTEWARD_NOTICE_INTERVAL`. At most 200 open notices of a kind are kept; dismissed ones are removed after 90 days.
