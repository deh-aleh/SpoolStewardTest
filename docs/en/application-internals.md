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
docker/         container definitions (stack/ is the production one)
deploy/         host install without docker: not supported
docs/           this documentation (en/ and it/)
requirements/   locked Python dependencies, with hashes (CONTRIBUTING.md)
scripts/        helper scripts
tests/          automated tests
docker-compose.yml  docker-compose.test.yml  pyproject.toml  alembic.ini  CONTRIBUTING.md
```

## 2. Configuration: who reads what

There is **one single** settings file, `.env`, made by `scripts/docker-init.sh` from `docker/env.example` (mode 600). It is read by **docker compose**
(part A: `PUID`, `PORTAL_PORT`, `CUPS_QUEUES`, the host folders, the version) and by the **gateway** and **mdns** containers (`env_file: .env`; part B: every
`SPOOLSTEWARD_*` setting, plus the `CERT_SYNC_*` options of `scripts/cert-sync.sh`). Every variable is described in [configuration.md](configuration.md).

The application ([`spoolsteward/config.py`](../../spoolsteward/config.py)) reads **only real environment variables** with the prefix `SPOOLSTEWARD_`, not a
`.env` file. Every setting has a safe default; an empty value is an error (`none` where a setting may be unset). **`docker-compose.yml` is never edited**, so an
update can replace it without erasing anyone's choices; an update never touches `.env` either. A test checks that every active value of `docker/env.example`
equals the default in `config.py`.

## 3. The Python package, file by file

### 3.1 Entry points

| File | Role |
|---|---|
| `spoolsteward/__main__.py` | The `spoolsteward` command: refuses to start without TLS, with half-configured RADIUS or with an unusable `SPOOLSTEWARD_ESCL_WINDOW_SECONDS`, then runs uvicorn. |
| `spoolsteward/main.py` | Builds the FastAPI app: CUPS client (unix socket), database, auth providers, middleware, routers, `/healthz`, background tasks. |
| `spoolsteward/cli.py` | The `spoolsteward-admin` command: `set-admin-password`, `group-limit`, `user-limit`, `set-group`, `make-admin`, `jobs`, `audit-archive`, `backup-key`, `backup-run`, `backup-verify`, `backup-restore`. |
| `spoolsteward/config.py` | `Settings` (pydantic-settings): every `SPOOLSTEWARD_*` variable with its default and validation. |
| `spoolsteward/cups_client.py` | The gateway's conversation with CUPS: lists queues in the display order; `queue_caps()` asks one queue whether it has colour and a duplex unit (cached 60 s) for the permission checks. |
| `spoolsteward/mdns.py` | Separate process (`python -m spoolsteward.mdns`): announces queues as `_ipps._tcp` (+ AirPrint subtype) and scanners as `_uscans._tcp`. |
| `spoolsteward/escl_window.py` | The scan window: which address may scan without credentials, for whom and for how long (in memory). |
| `spoolsteward/escl.py` | Scanning: eSCL proxy for native clients (`/eSCL/...`) and the portal scan page (`/me/scan`); discovers scanners at run time. |
| `spoolsteward/i18n.py` | Translation engine: language negotiation, `_()`, plurals, middleware. See [languages.md](languages.md). |

### 3.2 `spoolsteward/ipp/` — the IPP side

| File | Role |
|---|---|
| `codec.py` | Lossless IPP (RFC 8010) parser/serializer, nested collections included. `decode_head` reads only the header so documents stream without entering memory. |
| `gateway.py` | The reverse proxy: HTTPS check, authentication, operation filter, identity override, permissions (refuses or rewrites `print-color-mode` / `sides` before CUPS, filters the printer lists), quota reservation, streaming to CUPS, recording the outcome. |
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
| `permissions.py` | Advanced permissions: `permission_rule` of a group or user, `effective()` (group, then user, field by field; the emergency administrator is exempt), hours (`in_window`), and `decide()`, the pure function that says if a job is allowed, refused or corrected. It also adds the user's active `permission_grant` rows and notifies the end of a temporary permission. |
| `grants.py` | Quota, group and permission requests, approval (idempotent), direct grants, revocation, expiry of old requests, deletion of ended grants. |
| `rules.py` | Create/replace/remove limits for a user or group (shared by CLI and portal). |
| `pages.py` | Counts pages before printing (PDF, JPEG/PNG/GIF, URF, PostScript, text), parses page ranges, and turns the pages of a PDF to landscape (`landscape_pdf`). |
| `reconcile.py` | Reconciler loop with CUPS (real pages, canceled jobs, orphan reservations). |
| `control.py` | Gateway maintenance mode, per-printer block and mDNS hiding, the token for the mdns channel. |
| `keepalive.py` | Periodic maintenance print: settings, scheduler, test print. |
| `testpage.py` | Builds the PDFs the gateway prints itself (no dependency). |
| `ordering.py` | List order: physical first, then virtual; each by first-seen time. |
| `discovery.py` | Finds new USB printers (ipp-usb first, CUPS usb backend second), creates their queues, removes unreachable USB queues after `OFFLINE_REMOVE_HOURS`; remembers former queue names (`printer_former`). |

### 3.5 `spoolsteward/admin/` — the portal

| File | Role (URL prefix) |
|---|---|
| `static/` | Styles, scripts (`css/app.css` and `js/app.js` for every page, `css/<page>.css` and `js/<page>.js` for one page; no inline CSS or JavaScript in templates) and the icons (`icon.svg`, `icon-dark.svg` / `icon-light.svg` picked by theme, `favicon.ico`, `apple-touch-icon.png`, `icon-512.png`). Served without login at `/admin/static/`. |
| `routes.py` | Login/logout, first-run setup, audit list and CSV export, job cancel (`/admin/…`). |
| `accounts.py` | Users, groups, limits, advanced permissions (the form block and its checks), user keys, admin-action log (`/admin/users`, `/groups`, `/actions`). |
| `requests.py` | One page for requests and grants: pending requests, approval/rejection, active grants, direct grants, history, deleting ended grants (`/admin/requests`). |
| `reports.py` | Usage report: top users in a period, page / CSV / PDF (`/admin/report`). |
| `docs.py` | The documentation in the portal: the Markdown files of `docs/` as pages (`/docs`), in English or Italian. Administrators see every page; other users only `USER_PAGES` (user guide and Windows guide). Only names found in the folder are served; raw HTML is shown as text; links between pages are rewritten, links to source files go to GitHub at the tag of the running version. The folder is `/opt/spoolsteward/docs` in the image, `docs/` in a source tree. |
| `printers.py` | Printers overview, counters, test printers, keep-alive and test print (`/admin/printers`). |
| `controls.py` | Maintenance mode, block / hide a printer, `/mdns/policy` feed for the announcer. |
| `backups.py` | Backup targets (`/admin/backups`). |
| `audit_archive.py` | List/read/download/verify archives (`/admin/archive`). |
| `audit_view.py` | How one audit row is displayed (clipped text, device, print specs). |
| `me.py` | User area: consumption, keys, password, settings (theme, language, refresh), requests (`/me`). |
| `printing.py` | Print from the browser (`/me/print`): checks, quota, options, landscape handling, permissions (same `decide()` as the gateway), Print-Job to CUPS. |
| `../secheaders.py` (in `spoolsteward/`) | The security headers. The Content-Security-Policy (`SPOOLSTEWARD_CONTENT_SECURITY_POLICY`) forbids inline scripts and styles: pages keep their CSS and JavaScript in `static/`. Pages also get `Referrer-Policy`, `Permissions-Policy` and `X-Frame-Options`; every answer gets `nosniff`, and on HTTPS `Strict-Transport-Security`. |
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
| `spoolsteward/notices.py`, `spoolsteward/admin/notices.py` | The bell (table `notice`): notices for administrators, limited in how often the same one is reported (interval by `SPOOLSTEWARD_LOG_LEVEL`), and notices for one user. `notify_user_counted` renews the open notice of the same kind instead of adding rows; `SECONDARY_KINDS` (today `job_adjusted`) are marked read by `read_secondary` after `user.secondary_read_minutes` (default 20, 0 = never), on every page render. |
| `spoolsteward/secretcheck.py` | At every start: notice when `SPOOLSTEWARD_SECRET_KEY` is missing or has changed, and the one-time reminder to save it. What was already said is kept in `gateway_setting`, never the key. |
| `spoolsteward/printerwatch.py` | Every minute: paper jam, paper out, ink out/low on the physical printers, as notices for administrators and for the users waiting. |
| `spoolsteward/keywatch.py` | Every hour: forgets old print keys, if turned on warns of keys about to expire, and tells people whose temporary permission ended. |
| `spoolsteward/updates.py`, `spoolsteward/admin/updates.py` | Daily check for a newer release (note for administrators) and the portal button that hands an update to the `updater` container through files in `/data/update` (the gateway never touches Docker). |
| `spoolsteward/crypto_fields.py` | SQLAlchemy column types that encrypt the file names when `SPOOLSTEWARD_ENCRYPT_FILE_NAMES` is on (AES-GCM, key derived from `SPOOLSTEWARD_SECRET_KEY`); an unreadable value shows as `[encrypted]`. |
| `spoolsteward/titles.py` | Background task: when the encryption of names is on, encrypts the ones written in clear and, if enabled, erases names older than `SPOOLSTEWARD_JOB_TITLE_RETENTION_DAYS`. |
| `spoolsteward/certwatch.py` | Reads the TLS certificate and `sync-status.json`; opens and closes the certificate notices ([certificates.md](certificates.md)). |
| `spoolsteward/logsetup.py` | Log format: time zone of `SPOOLSTEWARD_TIMEZONE` with the UTC offset, level from `SPOOLSTEWARD_LOG_LEVEL`. |
| `spoolsteward/migrations/` | `env.py` and the revisions `0001`…`0017`. From `0014` on a migration may only add (a test checks it), so a rolled-back older version still starts on a newer database. |
| `spoolsteward/locales/` | `messages.pot` (template) and `<lang>/messages.po` (it, es, ru, zh). |

## 4. Scripts

An installation has the scripts for the server and for the users' computers. `make-release-package.sh`, `gen_dev_cert.sh` and
`i18n.py` are for developers: they are only in the repository, not in the release package.

| Script | Purpose |
|---|---|
| `scripts/docker-init.sh [names/IPs]` | One-time setup, in plain shell (needs `sed` and `openssl`): creates `.env` (with version, user and a generated secret), the `data/` folders and a self-signed TLS certificate for the names/IPs given. |
| `scripts/backup.sh backup\|restore` | Full **host-side** backup (database, config, audit archive, CUPS queues, `.env`); used to move the machine. |
| `scripts/add-printer-linux.sh` | Adds a gateway printer to a Linux computer with the print key stored in the queue address. |
| `scripts/print-test.sh` | Sends one small text page as a user (uses a sheet of paper). |
| `scripts/resources.sh` | CPU and RAM used by the containers and by Docker itself, added up. Run it with `watch -n 5` to follow it. |
| `scripts/gen_dev_cert.sh` | Development-only self-signed certificate. |
| `scripts/cert-sync.sh [--dry-run]` | From cron: copies the TLS certificate from the machine that renews it (`rsync` over `ssh`), checks it, installs it in `config/tls/` and restarts the gateway if it changed. Writes `config/tls/sync-status.json`, read by `certwatch.py`. See [certificates.md](certificates.md). |
| `scripts/make-release-package.sh VERSION FOLDER` | Builds the release package (`spoolsteward-<version>.tar.gz` + `SHA256SUMS`): compose, scripts, `docker/env.example`, documentation; never `.env`, `config/`, `data/`. |
| `scripts/update.sh [VERSION\|--check]` | Safe update: backup, downloads and checks (sha256) the release package, replaces the compose file and the scripts (never `.env`, `config/`, `data/`), pulls, restarts, waits for health, goes back to the previous files if needed and offers to restore the backup. It refuses to run in a folder with `.git`. `--from-portal` is the mode of the `updater` container. |
| `scripts/queue-restore.sh NAME [--reboot]` / `--list` | Brings back a deleted queue of `CUPS_QUEUES`: takes its name out of `/etc/cups/spoolsteward-queues` in the `cups` container; `--reboot` also restarts it. |
| `scripts/i18n.py` | Translation tooling: `extract`, `missing`, `check`, `add`. |

## 5. Containers

| Path | Purpose |
|---|---|
| `docker-compose.yml` | The production-style stack (`ipp-usb`, `cups`, `gateway`, `mdns`, optional `radius`). Builds from source or pulls published images. |
| `docker-compose.test.yml` | Test environment: a real CUPS on `:6310` and FreeRADIUS on `:1812`. |
| `docker/stack/Dockerfile` | The one image `spoolsteward`: the application, CUPS and ipp-usb. The services `gateway`, `cups`, `ipp-usb` and `mdns` start it with different commands. |
| `docker/stack/cups/` | CUPS: `cupsd.conf` (private backend policy), `entrypoint.sh` (creates the queues from `CUPS_QUEUES`, each only once). The drivers (brlaser, gutenprint, splix, foo2zjs) come from the Dockerfile. It also sees `/dev/bus/usb` for printers ipp-usb can not serve. |
| `docker/stack/ipp-usb/` | `ipp-usb.conf`, and the entrypoint that restarts it when USB devices change. |
| `docker/test/`, `docker/radius/` | `cupsd.conf` of the test CUPS (the production one plus a TCP listener); test RADIUS users (`mario`, `anna`, `admin`). |
| `docker/stack/updater/` | Optional image `spoolsteward-updater` (profile `autoupdate`): waits for `/data/update/request.json`, runs `scripts/update.sh --from-portal`, writes `status.json`. Has the Docker socket. |
| `docker/env.example` | The template of `.env`, with every option explained (part A for compose, part B for the program). It is part of the release package. |

## 6. Where to look when…

| You want to… | Look at |
|---|---|
| change how a quota is computed | `spoolsteward/policy/engine.py`, `spoolsteward/policy/audit.py` |
| allow/forbid an IPP operation | `spoolsteward/ipp/gateway.py` |
| change what colour / one-sided / printer / hours permissions do | `spoolsteward/policy/permissions.py` (`decide()`), then where it is applied: `spoolsteward/ipp/gateway.py` and `spoolsteward/admin/printing.py` |
| add a portal page | a router in `spoolsteward/admin/`, a template in `spoolsteward/admin/templates/` (styles and scripts in `static/css/` and `js/`, never inline), a menu entry in `base.html` |
| add a setting | `spoolsteward/config.py`, then `docker/env.example` (part B, with its explanation and the same default value: a test checks it) and [configuration.md](configuration.md) |
| change the database | `spoolsteward/db/models.py` **and** a new Alembic revision (a test checks they match) |
| translate a text | [languages.md](languages.md) |
