# How the application is built

[Documentation index](../README.md) · English · [Italiano](../it/funzionamento-applicazione.md)

A guided tour of the repository: what every source file is for, how configuration works (`.env`,
`SPOOLSTEWARD_*`), and what the scripts and container files do. For the big picture read [architecture.md](architecture.md) first.

## 1. Repository layout

```
spoolsteward/   the application (Python package)
  admin/        the web portal: routes + Jinja templates
  audit/        administrative log and audit archive
  auth/         accounts, passwords, print keys, RADIUS, login protection
  backup/       backup engine (files, encryption, scheduler)
  db/           models, sessions, migration runner
  ipp/          IPP codec, reverse proxy, URI rewriting
  locales/      translations (.po) for it, es, ru, zh
  migrations/   Alembic revisions
  policy/       quotas, grants, page counting, controls, keep-alive, ordering
  certwatch.py  cli.py  config.py  crypto_fields.py  cups_client.py  escl.py  escl_window.py  i18n.py  keywatch.py  logsetup.py  main.py  mdns.py  notices.py  printerwatch.py  secretcheck.py  titles.py  updates.py
deploy/         host install without docker: not supported in this release, not in the release package
docker/         container definitions (stack/ is the production one)
docs/           this documentation (en/ and it/)
requirements/   locked Python dependencies, with hashes (CONTRIBUTING.md)
scripts/        helper scripts
tests/          automated tests
docker-compose.yml  docker-compose.test.yml  pyproject.toml  alembic.ini  CONTRIBUTING.md
```

## 2. Configuration: who reads what

There is **one single** settings file, `.env`.

| File | Read by | Contains | Created by |
|---|---|---|---|
| **`.env`** (project folder) | **docker compose** (to build `docker-compose.yml`) **and** the **gateway** and **mdns** containers (`env_file: .env`) | part A: `PUID`, `PGID`, `PORTAL_PORT`, `CUPS_QUEUES`, `CUPS_VIRTUAL_QUEUE`, `AUDIT_ARCHIVE_HOST_PATH`, `BACKUP_HOST_PATH`, `BACKUP_DIR`, `SPOOLSTEWARD_REGISTRY`, `SPOOLSTEWARD_VERSION`. Part B: every `SPOOLSTEWARD_*` setting, including secrets. Plus the `CERT_SYNC_*` options of `scripts/cert-sync.sh` | `scripts/docker-init.sh` (from `docker/env.example`), mode 600 |

The application itself ([`spoolsteward/config.py`](../../spoolsteward/config.py)) reads **only real environment variables** with the prefix
`SPOOLSTEWARD_` (it does not read a `.env` file: that one belongs to compose). Every setting has a safe default; an empty value
is an error (write `none` where a setting may be left unset). After editing `.env` run `docker compose up -d` (recreates the containers that use it). **`docker-compose.yml` is never edited**: everything the user may change is in `.env`, so an update can replace the compose file without erasing anyone's choices. The extra variables passed to the containers (like `PUID`) are ignored by the application.

### 2.1 Part A of `.env` (docker compose variables)

The variables read by docker compose itself (`PUID`, `PGID`, `PORTAL_PORT`, `CUPS_QUEUES`, the host folders, the version) are listed in [configuration.md](configuration.md#11-part-a-docker-compose-variables).

The file is made from `docker/env.example` (by `scripts/docker-init.sh`, in plain shell: it fills in version, user, folder and generates the secret). An update **never touches `.env`**: new options work with their default value (the one in `spoolsteward/config.py`) even when missing, and `docker/env.example` (replaced by every update) lists them with their explanation. A test checks that every active value of `docker/env.example` equals the default in `spoolsteward/config.py`.

### 2.2 Part B of `.env` (`SPOOLSTEWARD_*` settings)

Every `SPOOLSTEWARD_*` setting, one variable at a time with its default and what it does, is in [configuration.md](configuration.md). The commented version is part B of [`docker/env.example`](../../docker/env.example) and the authoritative list is `spoolsteward/config.py`.

## 3. The Python package, file by file

### 3.1 Entry points

| File | Role |
|---|---|
| `spoolsteward/__main__.py` | The `spoolsteward` command: refuses to start without TLS, with half-configured RADIUS or with an unusable `SPOOLSTEWARD_ESCL_WINDOW_SECONDS`, then runs uvicorn. |
| `spoolsteward/main.py` | Builds the FastAPI app: HTTP client towards CUPS (unix socket), database, auth providers, middleware (sessions, language), routers, `/healthz`, and starts the background tasks. |
| `spoolsteward/cli.py` | The `spoolsteward-admin` command: `set-admin-password`, `group-limit`, `user-limit`, `set-group`, `make-admin`, `jobs`, `audit-archive`, `backup-key`, `backup-run`, `backup-verify`, `backup-restore`. |
| `spoolsteward/config.py` | `Settings` (pydantic-settings): every `SPOOLSTEWARD_*` variable with its default and validation. |
| `spoolsteward/cups_client.py` | The gateway's own conversation with CUPS: lists queues (state, colour, media, online/offline, virtual flag) in the display order; `queue_caps()` asks one queue whether it has colour and a duplex unit (cached 60 s) for the permission checks. |
| `spoolsteward/mdns.py` | Separate process (`python -m spoolsteward.mdns`): announces queues as `_ipps._tcp` (+ AirPrint subtype) and scanners as `_uscans._tcp`. |
| `spoolsteward/escl_window.py` | The scan window: which address may scan without credentials, for whom and for how long (in memory). |
| `spoolsteward/escl.py` | Scanning: eSCL proxy for native clients (`/eSCL/...`) and the portal scan page (`/me/scan`); discovers scanners at run time. |
| `spoolsteward/i18n.py` | Translation engine: language negotiation, `_()`, plurals, middleware. See [languages.md](languages.md). |

### 3.2 `spoolsteward/ipp/` — the IPP side

| File | Role |
|---|---|
| `codec.py` | Lossless IPP (RFC 8010) parser/serializer, nested collections included. `decode_head` reads only the header so documents stream without entering memory. |
| `gateway.py` | The reverse proxy: HTTPS check, capability queries, authentication, operation filter, identity override, permissions (colour, sides, printer, hours: refuses or rewrites `print-color-mode` / `sides` before CUPS, filters the printer lists), quota reservation, streaming to CUPS, recording the outcome. |
| `rewrite.py` | Rewrites internal CUPS URIs in responses so clients keep talking to the gateway; declares `uri-authentication-supported=basic` so Mopria sends credentials. |

### 3.3 `spoolsteward/auth/` — who you are

| File | Role |
|---|---|
| `service.py` | Decides who may log in where: `portal_login` (any enabled user; the row decides local vs. delegated) and `print_login` (a print key; RADIUS password only in `keys+radius`). |
| `users.py` | Find/create users, default group, `is_emergency()`, deletion (history kept). |
| `groups.py` | The two system groups `default` and `administrator`. |
| `bootstrap.py` | Creates `administrator` without a password and prints the one-time `FIRST RUN` code. |
| `passwords.py` | argon2id hashing and the password rules (min. 12 characters, not equal to the user name). |
| `keys.py` | Print keys: creation with a chosen or single-use life, sha256 hash, states, verification, switching off, extending, tidying. |
| `radius.py` | RADIUS provider (pyrad). `base.py`: the provider interface. `cache.py`: short cache of successful logins. |
| `throttle.py` | Login lock-out; kept in `/data/login-lockout.json`, so a restart does not reset it. |
| `dictionary` | RADIUS attribute dictionary needed by pyrad. |

### 3.4 `spoolsteward/policy/` — quotas and controls

| File | Role |
|---|---|
| `engine.py` | Windows (weekly/monthly in the configured time zone, DST-safe), effective rules, usage, grants summed to limits, `check_job` → `Decision` (English `reason` for audit/IPP, `text()` localized for the portal). |
| `audit.py` | `reserve()` (atomic, under lock; with `deny=` it records a refusal decided elsewhere, such as a permission, without looking at the quota) and `finalize()` of a job; attaches the document to a `Create-Job` job. |
| `permissions.py` | Advanced permissions: the rule of a group or user (`permission_rule`), `effective()` (group, then user, field by field; the emergency administrator is exempt), hours (`in_window`), and `decide()`, the pure function that says if a job is allowed, refused or corrected. It also adds the active `permission_grant` rows of the user, renames printers inside the lists, tells people when a temporary permission ended (`notify_expired`) and writes the user's notices. |
| `grants.py` | Quota, group and permission requests, approval (idempotent; a permission request makes a `permission_grant`), direct grants, revocation, expiry of old requests, and the deletion of ended grants of both kinds. |
| `rules.py` | Create/replace/remove limits for a user or group (shared by CLI and portal). |
| `pages.py` | Counts pages before printing (PDF, JPEG/PNG/GIF, URF, PostScript, text), parses page ranges, and turns the pages of a PDF to landscape (`landscape_pdf`). |
| `reconcile.py` | Reconciler loop with CUPS (real pages, canceled jobs, orphan reservations). |
| `control.py` | Gateway maintenance mode, per-printer block and mDNS hiding, the token for the mdns channel. |
| `keepalive.py` | Periodic maintenance print: settings, scheduler, test print. |
| `testpage.py` | Builds the PDFs the gateway prints itself (no dependency). |
| `ordering.py` | List order: physical first, then virtual; each by first-seen time. |
| `discovery.py` | Finds new USB printers (ipp-usb first, CUPS usb backend + driver second), creates their queues, tracks unreachable USB queues and removes them after `OFFLINE_REMOVE_HOURS`; remembers the names of removed and renamed queues (`printer_former`) for the rename dialog. |

### 3.5 `spoolsteward/admin/` — the portal

| File | Role (URL prefix) |
|---|---|
| `static/` | The styles and scripts of the pages (`css/app.css` and `js/app.js` for every page, `css/<page>.css` and `js/<page>.js` for one page; the templates contain no inline CSS or JavaScript) and the icon: `icon.svg` (browser tab: ink tile with a light ring, readable on any tab colour), `icon-dark.svg` / `icon-light.svg` (ink tile for light backgrounds, paper tile for dark ones; the portal header picks one by theme), `favicon.ico`, `apple-touch-icon.png`, `icon-512.png`. Served without login at `/admin/static/`; `/favicon.ico` redirects there. |
| `routes.py` | Login/logout, first-run setup, audit list and CSV export, job cancel (`/admin/…`). |
| `accounts.py` | Users, groups, limits, advanced permissions (the form block and its checks), user keys, admin-action log (`/admin/users`, `/groups`, `/actions`). |
| `requests.py` | One page for requests and grants: pending requests, approval/rejection, active grants, direct grants, history, deleting ended grants (`/admin/requests`). |
| `reports.py` | Usage report: top users in a period, page / CSV / PDF (`/admin/report`). |
| `docs.py` | The documentation in the portal: shows the Markdown files of `docs/` as pages (`/docs`), in English or Italian. Administrators see every page; other users only `USER_PAGES` (user guide and Windows guide), anything else is a 404 for them. Only names found in the folder are served; raw HTML in the files is shown as text; links between pages are rewritten, links to source files go to GitHub at the tag of the running version. The folder is `/opt/spoolsteward/docs` in the image, `$PREFIX/docs` in the host install, `docs/` in a source tree. |
| `printers.py` | Printers overview, counters, test printers, keep-alive and test print (`/admin/printers`). |
| `controls.py` | Maintenance mode, block / hide a printer, `/mdns/policy` feed for the announcer. |
| `backups.py` | Backup targets (`/admin/backups`). |
| `audit_archive.py` | List/read/download/verify archives (`/admin/archive`). |
| `audit_view.py` | How one audit row is displayed (clipped text, device, print specs). |
| `me.py` | User area: consumption, keys, password, settings (theme, language, refresh), requests (`/me`). |
| `printing.py` | Print from the browser (`/me/print`): checks, quota, options, landscape handling, permissions (same `decide()` as the gateway), Print-Job to CUPS. |
| `../secheaders.py` (in `spoolsteward/`) | The security headers. The Content-Security-Policy added to every HTML page (`SPOOLSTEWARD_CONTENT_SECURITY_POLICY`) forbids inline scripts and styles: pages must keep their CSS and JavaScript in `static/`. Pages also get `Referrer-Policy`, `Permissions-Policy` and `X-Frame-Options`; every answer gets `nosniff`, and on HTTPS `Strict-Transport-Security` (`SPOOLSTEWARD_HSTS_SECONDS`). |
| `common.py` | Shared pieces: templates, session and CSRF dependencies, flash messages, redirects, helpers. |
| `templates/*.html` | Jinja pages: `base.html` (layout, menu, theme), `macros.html`, one template per page (`docs.html` shows the documentation). |

### 3.6 Other packages

| File | Role |
|---|---|
| `spoolsteward/audit/adminlog.py` | `record()`: writes an `admin_audit` row (who did what). |
| `spoolsteward/audit/archive.py` | Size estimation, crash-safe archiving to `.jsonl.gz`, verification, streaming reader, the periodic archiver. |
| `spoolsteward/backup/core.py` | Create/verify/prune/restore backups; SQLite snapshot; tar.gz with `MANIFEST.json`; retention; the marker file and the time limit (`bounded`) for network disks. |
| `spoolsteward/backup/crypto.py` | Recovery-key encryption (X25519 + HKDF + AES-256-GCM; private key wrapped by scrypt of the passphrase). |
| `spoolsteward/backup/service.py` | `backup_target` bookkeeping and the 60 s scheduler. |
| `spoolsteward/db/models.py` | All SQLAlchemy models. `session.py`: engine and sessions (SQLite WAL). `migrate.py`: runs Alembic from inside the app. |
| `spoolsteward/notices.py`, `spoolsteward/admin/notices.py` | The bell: notices for administrators with a limit on how often the same one is reported (interval by `SPOOLSTEWARD_LOG_LEVEL`), and notices for one user; table `notice`. For users: `notify_user_counted` renews the open notice of the same kind and subject (counter, time) instead of adding rows; `SECONDARY_KINDS` (today `job_adjusted`, the forced prints) are marked read by `read_secondary` after `user.secondary_read_minutes` (default 20, 0 = never; `/me/settings`), run on every page render. |
| `spoolsteward/secretcheck.py` | At every start: notice when `SPOOLSTEWARD_SECRET_KEY` is missing or has changed (with how many file names it locked), and the one-time reminder to save it. What was already said is kept in `gateway_setting`, never the key itself. |
| `spoolsteward/printerwatch.py` | Every minute: paper jam, paper out, ink out/low on the physical printers, as notices for administrators and for the users waiting. |
| `spoolsteward/keywatch.py` | Every hour: forgets old print keys, if turned on warns of keys about to expire, and tells people whose temporary permission ended. |
| `spoolsteward/updates.py`, `spoolsteward/admin/updates.py` | Daily check for a newer release (note for administrators) and the portal button that hands an update to the `updater` container through files in `/data/update` (the gateway never touches Docker). |
| `spoolsteward/crypto_fields.py` | SQLAlchemy column types that encrypt the file names (AES-GCM, key derived from `SPOOLSTEWARD_SECRET_KEY`); an unreadable value shows as `[encrypted]`. |
| `spoolsteward/titles.py` | Background task: encrypts names written before the key existed and, if enabled, erases names older than `SPOOLSTEWARD_JOB_TITLE_RETENTION_DAYS`. |
| `spoolsteward/certwatch.py` | Reads the TLS certificate and `sync-status.json`; opens and closes the certificate notices ([certificates.md](certificates.md)). |
| `spoolsteward/logsetup.py` | Log format: time zone of `SPOOLSTEWARD_TIMEZONE` with the UTC offset, level from `SPOOLSTEWARD_LOG_LEVEL`. |
| `spoolsteward/migrations/` | `env.py` and the revisions `0001`…`0017` (schema history; `0015` adds `permission_rule` and `printer_former`, `0016` adds `permission_grant`, `0017` adds `user.secondary_read_minutes`). From `0014` on a migration may only add (expand/contract; a test checks it), so a rolled-back older version still starts on a newer database. |
| `spoolsteward/locales/` | `messages.pot` (template) and `<lang>/messages.po` (it, es, ru, zh). |

## 4. Scripts

An installation has the scripts for the server and for the users' computers. `make-release-package.sh`, `gen_dev_cert.sh` and
`i18n.py` are for developers: they are only in the repository, not in the release package.

| Script | Purpose |
|---|---|
| `scripts/docker-init.sh [names/IPs]` | One-time setup, in plain shell (needs `sed` and `openssl`): creates `.env` from `docker/env.example` (with version, user and a generated secret), the `data/` folders and a self-signed TLS certificate whose SAN lists the names/IPs given. |
| `scripts/backup.sh backup\|restore` | Full **host-side** backup (database, config, audit archive, CUPS queues, `.env`); used to move the machine. |
| `scripts/add-printer-linux.sh` | Adds a gateway printer to a Linux computer with the print key stored in the queue address. |
| `scripts/print-test.sh` | Sends one small text page as a user (uses a sheet of paper). |
| `scripts/resources.sh` | CPU and RAM used right now by the SpoolSteward containers and by Docker itself, added up (`docker stats` has no total). Run it with `watch -n 5` to follow it. |
| `scripts/gen_dev_cert.sh` | Development-only self-signed certificate. |
| `scripts/cert-sync.sh [--dry-run]` | From cron: copies the TLS certificate from the machine that renews it (`rsync` over `ssh`), checks it, installs it in `config/tls/` and restarts the gateway if it changed. Writes `config/tls/sync-status.json`, which the portal reads (`spoolsteward/certwatch.py`). See [certificates.md](certificates.md). |
| `scripts/make-release-package.sh VERSION FOLDER` | Builds the release package (`spoolsteward-<version>.tar.gz` + `SHA256SUMS`): compose, scripts, `docker/env.example`, documentation. Used by the release workflow; it never holds `.env`, `config/`, `data/`. |
| `scripts/update.sh [VERSION\|--check]` | Safe update with the published images: backup, downloads and checks (sha256) the release package, replaces the compose file and the scripts (never `.env`, `config/`, `data/`), new version in `.env`, pull, restart, waits for health, automatic return to the previous files and version and, if needed, an offer to restore the backup. It refuses to run in a folder with `.git`. `--from-portal` is the mode used by the `updater` container. |
| `scripts/queue-restore.sh NAME [--reboot]` / `--list` | Brings back a deleted queue of `CUPS_QUEUES`: takes its name out of `/etc/cups/spoolsteward-queues` in the `cups` container (the list of queues already created once); `--reboot` also restarts that container. |
| `scripts/i18n.py` | Translation tooling: `extract`, `missing`, `check`, `add`. |

## 5. Containers

| Path | Purpose |
|---|---|
| `docker-compose.yml` | The production-style stack (`ipp-usb`, `cups`, `gateway`, `mdns`, optional `radius`). Builds from source or pulls published images. |
| `docker-compose.test.yml` | Test environment: a real CUPS on `:6310` and FreeRADIUS on `:1812`. |
| `docker/stack/Dockerfile` | The one image `spoolsteward`: the application, CUPS and ipp-usb. The services `gateway`, `cups`, `ipp-usb` and `mdns` start it with different commands. |
| `docker/stack/cups/` | CUPS: `cupsd.conf` (private backend policy), `entrypoint.sh` (creates the queues from `CUPS_QUEUES`, each only once, remembered in `/etc/cups/spoolsteward-queues`), the printer drivers (brlaser, gutenprint, splix, foo2zjs) are installed by the Dockerfile above. It also sees `/dev/bus/usb` for printers ipp-usb can not serve. |
| `docker/stack/ipp-usb/` | `ipp-usb.conf`, and the entrypoint that restarts it when USB devices change. |
| `docker/test/`, `docker/radius/` | `cupsd.conf` of the test CUPS (the production one plus a TCP listener; the test CUPS is built from the production image); test RADIUS users (`mario`, `anna`, `admin`). |
| `docker/stack/updater/` | Optional image `spoolsteward-updater` (profile `autoupdate`): waits for `/data/update/request.json`, runs `scripts/update.sh --from-portal`, writes `status.json`. Has the Docker socket. |
| `docker/env.example` | The template of `.env`, with every option explained (part A for compose, part B for the program). It is part of the release package. |
| `deploy/` | Installation on the host without docker (systemd). Not supported in this release and not in the release package ([deploy/README.md](../../deploy/README.md)). |

## 6. Where to look when…

| You want to… | Look at |
|---|---|
| change how a quota is computed | `spoolsteward/policy/engine.py`, `spoolsteward/policy/audit.py` |
| allow/forbid an IPP operation | `spoolsteward/ipp/gateway.py` |
| change what colour / one-sided / printer / hours permissions do | `spoolsteward/policy/permissions.py` (`decide()`), then where it is applied: `spoolsteward/ipp/gateway.py` and `spoolsteward/admin/printing.py` |
| add a portal page | a router in `spoolsteward/admin/`, a template in `spoolsteward/admin/templates/` (styles and scripts in `spoolsteward/admin/static/css/` and `js/`, never inline), a menu entry in `base.html` |
| add a setting | `spoolsteward/config.py`, then `docker/env.example` (part B, with its explanation and the same default value: a test checks it) and [this page](#22-part-b-of-env-spoolsteward_-settings) |
| change the database | `spoolsteward/db/models.py` **and** a new Alembic revision (a test checks they match) |
| translate a text | [languages.md](languages.md) |
