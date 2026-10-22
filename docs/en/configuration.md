# Configuration reference (`.env`)

[Documentation index](../README.md) · English · [Italiano](../it/configurazione.md)

Every setting of SpoolSteward lives in one file, `.env`, in the project folder. `scripts/docker-init.sh` makes it from
[`docker/env.example`](../../docker/env.example) and sets mode 600 (it holds secrets). This page lists **all** the variables. The
authoritative list, with the defaults, is [`spoolsteward/config.py`](../../spoolsteward/config.py); a test checks that every setting there is described here.

How to read this page:

- **Default** is the value used when the variable is missing. You only write the lines you want to change.
- **A line is never left empty.** `NAME=` stops the start. A setting that may be switched off or left unset takes the word `none`
  (`SPOOLSTEWARD_RADIUS_HOST=none`); the table says which ones. For all the others `none` is refused: they need a real value.
- **After any change**, run `docker compose up -d` in the project folder: it recreates the containers that read `.env`. The settings are
  read once, when the program starts, so a change has no effect before that. Nothing else is needed (never edit `docker-compose.yml`).
- A value with spaces, `$` or `#` goes between single quotes: `SPOOLSTEWARD_RADIUS_SECRET='my$secret #1'`.
- `true` / `false`, in lower case, are the only values for on/off settings. `LOG_LEVEL` is written in upper case (`INFO`).
- Every value is checked when the portal starts: a number that is not a number (`SPOOLSTEWARD_BACKUP_HOUR=night`), a number outside its range,
  a word that is not in the list of accepted ones, an empty line. The portal does **not** start and `docker compose logs gateway` lists every
  wrong line at once, with the value it found (never a secret). Nothing is corrected silently.
- An update never changes your `.env`. A new version can bring new settings: they work with their default even if they are not in your
  file; `docker/env.example` (replaced by every update) shows them with an explanation.
- The two containers `gateway` and `mdns` read the same file. Settings that matter to the announcer are marked in the text.

The file has two parts. **Part A** is read by docker compose itself (which version, which user, which port). **Part B** is read by the
program and every variable starts with `SPOOLSTEWARD_`.

## 1. The secret key

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_SECRET_KEY` | made by `docker-init.sh` | A long random text that only this server knows. See below. |

It does three things:

- it **encrypts the file names** kept in the audit ([data-protection.md](data-protection.md));
- it **signs the portal session** (the cookie that says who is logged in);
- it **protects the channel** between `gateway` and `mdns` (what the announcer may publish).

It does not encrypt the rest of the database, and it is nobody's password: you never type it.

**Keep a copy of it outside the server** (a password manager is fine). An unencrypted backup does **not** contain it, on purpose. If it
is changed or lost, the file names already saved appear as `[encrypted]` for good (users, quotas, pages and dates are not affected) and
everybody has to log in again. If it is `none`, the gateway makes a new one at every start: sessions end at each restart, the channel to
`mdns` is not protected and the names are stored without encryption. Always set it in production. The portal warns the administrators
in the bell in these cases. How to save it and what an encrypted backup does: [backup.md](backup.md#save-the-secret-key).

To make one by hand: `openssl rand -hex 32`. The key must be **at least 64 characters** long, the length that command prints: a shorter
one stops the start. Never share it and never put it in a git repository: every installation needs its own. If the line is missing from
`.env`: [troubleshooting.md](troubleshooting.md#no-secret-key-is-set).

## 2. Accounts and login

### RADIUS (optional)

RADIUS lets people log in with their office password. With both `SPOOLSTEWARD_RADIUS_HOST` and `SPOOLSTEWARD_RADIUS_SECRET` set to `none` only local
accounts exist. If you fill only one of the two, the gateway **refuses to start** (a half login system is worse than none). The RADIUS
server must list this machine as a client, with the same secret. If RADIUS does not answer, the login is **denied** (it fails closed).

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_RADIUS_HOST` | `none` | Name or address of the RADIUS server. Example: `radius.office.lan` |
| `SPOOLSTEWARD_RADIUS_PORT` | `1812` | UDP port of the server. Change it only if yours is different. |
| `SPOOLSTEWARD_RADIUS_SECRET` | `none` | Secret shared with the server (not a user password). Quote it if it has `$` or `#`. |
| `SPOOLSTEWARD_RADIUS_TIMEOUT` | `5` | Seconds to wait for an answer. Raise it for a slow or distant server. |
| `SPOOLSTEWARD_AUTH_CACHE_SECONDS` | `60` | A good login is remembered this long, so the server is not asked at every request. |
| `SPOOLSTEWARD_PASSWORD_CHECKS_AT_ONCE` | `3` | How many password checks of local accounts run at the same time (1–16); the others wait. Each uses 32 MiB for a few tenths of a second (more on a Raspberry Pi), so a burst of login attempts slows down instead of using all the memory of a small machine. Raise it on a big machine. |

### Print authentication and keys

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_IPP_AUTH` | `keys` | What a **print client** (computer, phone) may present as password. `keys` = only personal print keys (recommended: the office password never gets saved on a device and a lost key can be revoked). `keys+radius` = also the RADIUS password, only for a transition period (RADIUS accounts only; anything starting with `cpk-` is treated as a print key and never sent to RADIUS). The web portal always needs the password. |
| `SPOOLSTEWARD_KEY_TTL_DAYS_RADIUS` | `90` | The **longest** life a RADIUS user can choose for a print key, in days (the menu shows only the choices that fit). Short on purpose: to renew a key they must log in to RADIUS again, so someone who left the company stops printing when the key ends. |
| `SPOOLSTEWARD_KEY_TTL_DAYS_LOCAL` | `730` | The same, for local accounts. |
| `SPOOLSTEWARD_KEY_ONCE_HOURS` | `24` | A "one print only" key that nobody used stops working after this many hours. |
| `SPOOLSTEWARD_KEY_ONCE_GRACE_SECONDS` | `120` | After its print, a one-print key can still answer "how is my job going?" for this long (phones and PCs ask). |
| `SPOOLSTEWARD_KEY_HISTORY_DAYS` | `14` | Revoked and expired keys disappear from the list this many days after they ended. `0` = they stay for ever. |
| `SPOOLSTEWARD_KEY_EXPIRY_WARN_ENABLED` | `false` | `true` tells the owner in the bell before a key expires. |
| `SPOOLSTEWARD_KEY_EXPIRY_WARN_DAYS` | `7` | How many days before the end that notice appears (only when the line above is on). |
| `SPOOLSTEWARD_AUTO_CREATE_USERS` | `true` | An unknown RADIUS user who logs in to the portal is created in the default group. `false` = register users first; anyone not on the list cannot enter. |
| `SPOOLSTEWARD_DEFAULT_GROUP` | `default` | The group of new users, and where users go when their group is deleted. |
| `SPOOLSTEWARD_CUPS_USER_PREFIX` | `gw:` | Put before the user name CUPS sees (`gw:mario`). It cannot equal a local account of the server, so a RADIUS user called `root` does not become a CUPS administrator. Do not change it without a reason. |
| `SPOOLSTEWARD_ANONYMOUS_DISCOVERY` | `true` | Answers "what printer are you?" without a login. IPP Everywhere clients (Linux, Android, macOS) ask the printer before they have credentials. It does not allow printing and does not show names or documents. Set `false` only if you know your clients do not need it. |
| `SPOOLSTEWARD_CONTENT_SECURITY_POLICY` | `enforce` | The browser rule that forbids inline scripts and styles in the portal pages, so a script slipped into a name or a message is not run. `report-only`: the browser only writes what it would block in its console (for finding out why a page misbehaves behind a proxy that rewrites pages). `off`: no rule. |
| `SPOOLSTEWARD_HSTS_SECONDS` | `0` | Seconds a browser must use only HTTPS with this **host name** (header `Strict-Transport-Security`, sent only on HTTPS answers; `0` = never sent, the default). A browser keeps it only if it **trusts the certificate**: with the self-signed one you click through, it is ignored. Once kept, `http://` addresses become `https://` and a certificate error can not be clicked through while the certificate is wrong; it applies to every port of that name. Off by default; with a trusted certificate try `2592000` (30 days), later `31536000` (one year). See [certificates.md](certificates.md). |

### First start

On the first start an emergency administrator exists **without a password**. The gateway prints a one-time code in the log; with it you
choose the password at `https://<server>:8631/setup` (see [installation.md](installation.md)). The code works once.

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_BOOTSTRAP_ADMIN` | `true` | Creates that account (no password, no group, no limits) when it does not exist. `false` = it is not created; then create an administrator with `spoolsteward-admin set-admin-password NAME`. The name stays reserved either way. |
| `SPOOLSTEWARD_BOOTSTRAP_ADMIN_USERNAME` | `administrator` | Its name. Also the person who gets the reminder to save the secret key. |

### Requests for more quota

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_MAX_OPEN_REQUESTS` | `3` | How many requests one user can have open at the same time. |
| `SPOOLSTEWARD_REQUEST_TTL_DAYS` | `14` | After how many days a request nobody answered expires. |
| `SPOOLSTEWARD_REQUEST_REASON_REQUIRED` | `false` | `true` = a user must write a reason when asking for quota, a permission or a group change; `false` = the reason is optional. |

### Advanced permissions

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_PERMISSION_MODE` | `adjust` | What happens when a program **explicitly** asks for colour, or for a one-sided print, that the user may not have. `adjust`: the job is printed in black and white / double-sided and the user gets a notice. `refuse`: the job is refused. Printers and hours always refuse. A program that did not ask is corrected without a notice in both modes. Windows and Android often ask for one-sided or colour in every job, so `refuse` refuses many of their jobs ([quotas.md](quotas.md#5-advanced-permissions)). Any other value stops the gateway at start. |

### Failed logins

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_LOCKOUT_FILE` | `/data/login-lockout.json` | Failed logins are counted per address and kept in this file inside the container (the `/data` volume), so a restart does not end a lock-out. `none` = memory only. Leave it as it is with docker; the host install (without docker) sets `/var/lib/spoolsteward/login-lockout.json` in its `deploy/env.example`. |

## 3. Audit and file names

The audit is never deleted from the portal. When the active tables grow over the limit below, the oldest rows are written to
compressed files, checked, and only then removed from the database ([audit.md](audit.md)).

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_AUDIT_MAX_MB` | `50` | Size of the active audit that triggers archiving. Raise it for a bigger database, lower it to archive earlier. |
| `SPOOLSTEWARD_AUDIT_ARCHIVE_DIR` | `/data/audit-archive` | Folder of the compressed files **inside the container**. The compose file maps it to `./data/audit-archive` on the host: change that mapping, not this. |
| `SPOOLSTEWARD_AUDIT_ARCHIVE_HOST_PATH` | set by init | Only **shown** in the portal, so people know where the files are on this machine. It is filled from `AUDIT_ARCHIVE_HOST_PATH` of part A. |
| `SPOOLSTEWARD_AUDIT_CHECK_INTERVAL` | `3600` | Seconds between the checks "is archiving needed?" |
| `SPOOLSTEWARD_DB_SYNC` | `full` | How carefully the database saves. `full` = after every save the program waits for the disk: a power cut loses nothing. `normal` = it does not wait: faster on a slow disk (the SD card of a Raspberry Pi), but after a power cut (or a crash of the whole machine, not of the program) the last few seconds of saves can be missing, for example a print just accepted. The database is not damaged either way. Change it only if printing or the portal feel slow on a small machine. |
| `SPOOLSTEWARD_JOB_TITLE_RETENTION_ENABLED` | `false` | `true` erases the names of printed files after the days below. The row (who, when, pages, state) stays, because quotas need it. Names already inside archive files are not touched. |
| `SPOOLSTEWARD_JOB_TITLE_RETENTION_DAYS` | `90` | After how many days the name is erased (only when the line above is on). |

The names are encrypted with a key taken from `SPOOLSTEWARD_SECRET_KEY` ([data-protection.md](data-protection.md)).

## 4. Backup

The *general* backup is the one set here. Each administrator makes their own in the portal (page *Backup*), with nothing to write in
`.env`. Everything is written inside one folder (`/backups` in the container, `./data/backups` on the host). Details, encryption and
restore: [backup.md](backup.md).

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_BACKUP_ENABLED` | `false` | Turns the general backup on. |
| `SPOOLSTEWARD_BACKUP_DIR` | `general` | Its sub-folder inside the backup folder. |
| `SPOOLSTEWARD_BACKUP_HOUR` | `3` | Local hour (0-23) of the daily run. If the gateway was off then, it runs as soon as it starts. |
| `SPOOLSTEWARD_BACKUP_KEEP_DAILY` | `7` | Copies kept: the last one of each of the last N days. |
| `SPOOLSTEWARD_BACKUP_KEEP_WEEKLY` | `4` | The last one of each of the last N weeks. |
| `SPOOLSTEWARD_BACKUP_KEEP_MONTHLY` | `12` | The last one of each of the last N months. Older copies are deleted only after the new one was written and read back. |
| `SPOOLSTEWARD_BACKUP_REQUIRE_MARKER` | `false` | `true` writes only if the file `.spoolsteward-destination` exists in the folder. Use it when the folder is a USB disk or a network share: if the disk is not mounted the backup fails (red in the portal) instead of filling the server disk. Make the file once, with the disk mounted: `touch data/backups/<folder>/.spoolsteward-destination`. |
| `SPOOLSTEWARD_BACKUP_RECIPIENT` | `none` | The recovery key made by `spoolsteward-admin backup-key` (`cpgbk1.xxxx.yyyy`). `none` = backups are **not encrypted** and contain **no secrets** (not the secret key, the TLS key, the RADIUS secret). |
| `SPOOLSTEWARD_BACKUP_ROOT` | `/backups` | The only folder the gateway may write backups to, **inside the container**. Mapped from `BACKUP_DIR` of part A. Leave it. |
| `SPOOLSTEWARD_BACKUP_HOST_PATH` | set by init | Only shown in the portal: where `/backups` is on the host. Filled from `BACKUP_HOST_PATH` of part A. |
| `SPOOLSTEWARD_BACKUP_CONFIG_DIR` | `/config` | Read-only copy of `config/` (TLS) inside the container. Saved only in encrypted backups. Leave it. |
| `SPOOLSTEWARD_BACKUP_ENV_FILE` | `/project.env` | Read-only copy of `.env` inside the container. Saved only in encrypted backups. Leave it. |

## 5. Updates

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_UPDATE_CHECK` | `true` | Once a day the gateway asks GitHub for the newest release and, if it is newer, shows a note to administrators. Nothing is updated by itself and the request sends nothing about your server. `false` = never ask. |
| `SPOOLSTEWARD_UPDATE_BETA` | `true` | `true`: betas (GitHub pre-releases, for example `0.0.0-beta.2`) are offered like final releases, in the note, by the portal's *Update* button and by `scripts/update.sh` without arguments; installing one asks for one more confirmation. `false`: only final releases. See [installation.md](installation.md#5-updates). |
| `SPOOLSTEWARD_UPDATE_REPOSITORY` | `deh-aleh/SpoolSteward` | The GitHub repository, written `owner/name`, whose releases are checked and installed: the note, the portal's *Update* button and `scripts/update.sh` all use it (release list, package download, link to the release notes). Change it only for a fork. |
| `SPOOLSTEWARD_UPDATE_DIR` | `/data/update` | Folder shared with the optional `updater` container (`docker compose --profile autoupdate`). Leave it. |

How to update: [installation.md](installation.md).

## 6. Printing and scanning

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_WEB_UPLOAD_MB` | `25` | Biggest file accepted when printing from the portal (phones). |
| `SPOOLSTEWARD_WEB_MAX_COPIES` | `20` | Most copies in one job from the portal. |
| `SPOOLSTEWARD_MAX_REQUEST_MB` | `200` | An IPP job (document included) bigger than this is refused. A reverse proxy in front must allow at least as much. |
| `SPOOLSTEWARD_RECONCILE_INTERVAL` | `30` | Seconds between the comparisons of the gateway's jobs with CUPS (this is how quotas and real page counts are corrected). If CUPS cannot be reached, `reconcile:` appears in the log. |
| `SPOOLSTEWARD_SCAN_URL` | the image sets `http://ipp-usb:60000` | eSCL scanners. Scanners are found by themselves; write this only to **add** some that are not (a network printer with eSCL on an unusual port), as `name=address` separated by commas: `http://ipp-usb:60000,office=https://192.168.1.50:443`. `none` = scanning is off. |
| `SPOOLSTEWARD_SCAN_TIMEOUT` | `180` | Seconds for one scan (a page from the glass can be slow). |
| `SPOOLSTEWARD_SCAN_KEEP_MB` | `50` | Megabytes of finished portal scans kept in memory, **all people together**. When a new scan does not fit, the oldest finished ones are dropped first; the newest is always kept, even if it alone is bigger. |
| `SPOOLSTEWARD_SCAN_KEEP_MINUTES` | `10` | Minutes a finished portal scan stays in memory. After that it is gone and must be scanned again. |
| `SPOOLSTEWARD_SCAN_DELETE_AFTER_DOWNLOAD` | `false` | `true` = a scan leaves the memory and the portal as soon as its download has finished (it can be downloaded once). |
| `SPOOLSTEWARD_ESCL_ANONYMOUS` | `false` | `true` accepts scans from apps that cannot send credentials (for example NAPS2). They are credited to the guest account (next line) with the sender's IP. Leave `false` unless your network is trusted ([scanning.md](scanning.md)). |
| `SPOOLSTEWARD_ESCL_GUEST_USERNAME` | `unknown-guest` | The name of that guest account: local, no password, created by itself. If an account with this name already exists and is a real one (a password, RADIUS, or an administrator), it is never used: scans without login are refused, the administrators get a notice in the bell and the log says why. Choose another name then. Lower-case letters, digits, dot, dash, underscore, up to 63; the name of the emergency administrator is refused at start. |
| `SPOOLSTEWARD_ESCL_WINDOW` | `true` | `true` shows a button on *Scan* that opens a **scan window**: for a few minutes, scan requests without credentials from the IP address of the person who pressed it are accepted as that person. A last resort: NAT and reverse proxies defeat it (limits in [scanning.md](scanning.md)). `false` removes the button. Works with `SPOOLSTEWARD_ESCL_ANONYMOUS` on or off ([scanning.md](scanning.md)). |
| `SPOOLSTEWARD_ESCL_WINDOW_SECONDS` | `180` | How long the window stays open: **60 to 900** seconds. Any other value stops the portal at start (`docker compose logs gateway`). A scan that has started is finished even if the time ends. |

## 7. Printers

### The Printers page

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_PRINTERS_REFRESH_SECONDS` | `15` | How long what the printers say is reused, and how often the page reloads itself when an administrator turns on "Auto refresh". From 5 to 3600; any other value stops the start. The lower it is, the more often the printers are asked. |
| `SPOOLSTEWARD_PRINTER_ALERTS_USERS` | `true` | A paper jam, no paper or no ink/toner is always shown to the administrators (the bell). With `true` the people who have a print waiting on that printer are told too, in their own bell, and only them. `false` = administrators only. |

### USB printers: finding and removing

A new USB printer gets its queue in CUPS by itself within about a minute: through the `ipp-usb` bridge when the printer speaks IPP over USB
(most recent ones, no driver needed), otherwise through CUPS' own `usb` backend (many older lasers; it needs a driver CUPS knows).
A printer served by `ipp-usb` is never taken the second way. See [printers.md](printers.md).

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_AUTO_DISCOVER` | `true` | Look for printers by itself. `false` = only the queues you create. |
| `SPOOLSTEWARD_DISCOVERY_INTERVAL` | `30` | Seconds between the looks (and the check of which USB printers do not answer). |
| `SPOOLSTEWARD_USB_BRIDGE_HOST` | `ipp-usb` | Where the `ipp-usb` bridge is. `none` = do not look there. |
| `SPOOLSTEWARD_USB_BRIDGE_PORTS` | `60000-60099` | The ports `ipp-usb` gives to printers (`http-min-port`..`http-max-port` of its configuration). |
| `SPOOLSTEWARD_USB_DIRECT` | `true` | Also look for USB printers that `ipp-usb` does not serve. `false` = CUPS does not touch the USB bus. |
| `SPOOLSTEWARD_OFFLINE_REMOVE_HOURS` | `8` | A USB queue whose device has not answered for this many hours is removed by itself (the Printers page shows a countdown). `0` = never. Network queues are **never** removed by themselves: a printer switched off for the weekend comes back as it was. |

## 8. Network and announcement (mDNS)

`mdns` is the container that announces the printers on the local network (Bonjour, AirPrint, Mopria). It uses the host network and
reads the same `.env`.

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_PORTAL_URL` | `none` | The public address of the portal, used in messages ("ask for more quota at ..."). `none` = taken from the request; for messages shown by print clients it is better to write it. Example: `https://print.example.com:8631` |
| `SPOOLSTEWARD_MDNS_HOSTNAME` | `none` | The name the announcer publishes. It must be one of the names inside the TLS certificate, or clients refuse to connect. `none` = the host part of `SPOOLSTEWARD_PORTAL_URL`, otherwise the machine name. |
| `SPOOLSTEWARD_MDNS_ADDRESSES` | `none` | IPv4 addresses to announce, separated by commas. `none` = the one of the default route. Needed if the machine has several network cards and the right one is not the first. |
| `SPOOLSTEWARD_MDNS_PORT` | `0` | `0` = the port of the gateway. The compose file sets it from `PORTAL_PORT`: do not set it by hand. |
| `SPOOLSTEWARD_MDNS_INTERVAL` | `30` | Seconds between the looks at the queue list (an unplugged or hidden printer disappears from the network within this time). |
| `SPOOLSTEWARD_PUBLIC_SCHEME` | `none` | Write `ipps` only behind a reverse proxy that ends the TLS. Otherwise write `none`: it is taken from the request. Accepted: `http`, `https`, `ipp`, `ipps`. |

## 9. TLS certificate: warnings

The gateway only looks at the certificate in `config/tls`; renewing it is done elsewhere (`scripts/cert-sync.sh`, see
[certificates.md](certificates.md)). "The certificate has expired" and "the last run of cert-sync.sh failed" are always reported.

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_CERT_NAME_CHECK` | `true` | Warns when the address announced on the network is not in the certificate. `false` turns this warning off. |
| `SPOOLSTEWARD_CERT_EXPIRY_WARN_ENABLED` | `false` | `true` warns before the certificate expires. Off by default: a renewal that is simply not due yet would look like a problem. |
| `SPOOLSTEWARD_CERT_EXPIRY_WARN_DAYS` | `14` | How many days before the end. Let's Encrypt renews about 30 days before, so a value below 30 means "the renewal has been failing for a while". |

## 10. Other

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_TIMEZONE` | `UTC` | The time zone of the quota weeks and months, of the times in the portal and of the logs. **Change it to yours**, for example `Europe/Rome` or `America/New_York` ([list](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)). |
| `SPOOLSTEWARD_LANGUAGE` | `en` | Portal language for browsers that do not say theirs and for users who chose none: `en`, `it`, `es`, `ru`, `zh` ([languages.md](languages.md)). Each user can pick their own in *Settings*. |
| `SPOOLSTEWARD_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, in upper case. `DEBUG` is very talkative: use it to look into a problem. |
| `SPOOLSTEWARD_HEALTHZ_INFO` | `none` | What `/healthz` shows besides the status. It needs **no login**, so anyone who reaches the port can read it. `none` = only the status. Add, separated by commas: `version` (the version that runs), `printers` (how many queues: usb, network, virtual; never names). Example: `version,printers`. |
| `SPOOLSTEWARD_NOTICE_INTERVAL` | `0` | How often the same notice from the same source is reported again, so a client that retries 100 times does not flood the bell and the log. `0` = by log level: `DEBUG` 1 minute, `INFO` 1 day, `WARNING` 3 days, `ERROR` 7 days. Write a number of seconds to choose your own. |

## 11. Part A: docker compose variables

These are read by docker compose itself, not by the program.

| Variable | Default | What it does |
|---|---|---|
| `SPOOLSTEWARD_REGISTRY` | `ghcr.io/deh-aleh` | Where the ready-made images come from. Leave it. Building the images from source is for developers ([CONTRIBUTING.md](../../CONTRIBUTING.md)). |
| `SPOOLSTEWARD_VERSION` | the installed version | Which version runs. Do not change it by hand: `scripts/update.sh` does it, after a backup. |
| `PUID`, `PGID` | your user | The Linux user that runs the gateway. It must be able to read the TLS key and write in `data/`. Filled in for you. |
| `PROJECT_DIR` | the project folder | Full path of this folder. Only the optional `updater` container uses it. |
| `PORTAL_PORT` | `8631` | The port people connect to (`https://<server>:PORT`). Change it if 8631 is taken; `631` if nothing else uses it. |
| `CUPS_QUEUES` | `none` | Printers created in CUPS at start, as `NAME=ADDRESS` separated by spaces. Each is created once: if you delete the queue later, it does not come back (`scripts/queue-restore.sh NAME --reboot` brings it back). USB printers do not need to be listed; list one only to give it a fixed name. Examples: `CUPS_QUEUES="OFFICE=ipp://192.168.1.60/ipp/print"`, `CUPS_QUEUES=HP7305=ipp://ipp-usb:60000/ipp/print` |
| `CUPS_VIRTUAL_QUEUE` | `0` | `1` also creates a test queue that writes PDF files instead of printing (to try quotas without paper). Use `0` in production. |
| `AUDIT_ARCHIVE_HOST_PATH` | `<project>/data/audit-archive` | Only shown in the portal (see `SPOOLSTEWARD_AUDIT_ARCHIVE_HOST_PATH`). |
| `BACKUP_HOST_PATH` | `<project>/data/backups` | Only shown in the portal. |
| `BACKUP_DIR` | `./data/backups` | The host folder mounted at `/backups`: the only place backups may go. To save on a USB disk or a share, mount it **inside** this folder. |
| `COMPOSE_PROFILES` | not set | `autoupdate` starts the optional `updater` container (the "Update" button in the portal; it needs the Docker socket, read the warning in [installation.md](installation.md) first). |
| `FORWARDED_ALLOW_IPS` | not set | Only behind a reverse proxy: the proxy's address, so the gateway trusts `X-Forwarded-For` (see [certificates.md](certificates.md#41-behind-a-reverse-proxy)). |

### Certificate synchronisation (`CERT_SYNC_*`)

Only `scripts/cert-sync.sh` reads these (the program does not). They copy the TLS certificate from the machine that renews it (Nginx Proxy
Manager, certbot) into `config/tls` and restart the gateway when it changed. Leave them out if you do not use it. Step by step, with
the read-only user: [certificates.md](certificates.md).

| Variable | Default | What it does |
|---|---|---|
| `CERT_SYNC_HOST` | none (required) | Name or address of the machine that holds the certificate. Example: `npm.example.lan` |
| `CERT_SYNC_USER` | none (required) | The ssh user on that machine: one that can only read. |
| `CERT_SYNC_KEY` | none (required) | Full path of the ssh private key of this machine. |
| `CERT_SYNC_REMOTE_DIR` | none (required) | Full path, on that machine, of the folder with the two files. Nginx Proxy Manager: `.../data/letsencrypt/live/npm-3`; certbot: `/etc/letsencrypt/live/yourdomain`. |
| `CERT_SYNC_PORT` | `22` | The ssh port. |
| `CERT_SYNC_CERT_FILE` | `fullchain.pem` | Name of the certificate file (the full chain). Only a plain file name. |
| `CERT_SYNC_KEY_FILE` | `privkey.pem` | Name of the private key file. Only a plain file name. |

## 12. Set by the image: do not change

The image and `docker-compose.yml` decide these. They are listed so you know what they are; writing them in `.env` can stop the gateway
from starting or open a security hole.

| Variable | Value in the container | Why |
|---|---|---|
| `SPOOLSTEWARD_TLS_CERTFILE`, `SPOOLSTEWARD_TLS_KEYFILE` | `/tls/fullchain.pem`, `/tls/privkey.pem` | The folder `config/tls`. Without TLS the gateway refuses to start. |
| `SPOOLSTEWARD_DATABASE_URL` | `sqlite:////data/spoolsteward.db` | The SQLite file in the `portal-data` volume. Only SQLite is supported: any other kind of database (PostgreSQL, MySQL…) is refused at start. |
| `SPOOLSTEWARD_CUPS_URL` | `unix:///run/cups/cups.sock` | The private socket shared with CUPS. CUPS is never reachable from the network. |
| `SPOOLSTEWARD_ALLOW_INSECURE_AUTH` | `false` | `true` would accept passwords over plain HTTP. Only for tests in a closed lab. Never in production. |
| `SPOOLSTEWARD_HOST`, `SPOOLSTEWARD_PORT` | `0.0.0.0`, `8631` | Where the program listens **inside** the container. To change the port people use, change `PORTAL_PORT` (part A). |
