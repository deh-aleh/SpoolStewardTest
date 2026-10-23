# Architecture

[Documentation index](../README.md) · English · [Italiano](../it/architettura.md)

Components, trust boundaries and the path of a request. For the role of each source file see [application-internals.md](application-internals.md).

## 1. The idea

CUPS prints, but it knows nothing about *who* is allowed to print *how much*. SpoolSteward is a layer placed in front of
CUPS that adds authentication, quotas, an audit trail and a web portal, **without modifying CUPS, ipp-usb or the printer
firmware**: they stay plain dependencies that can be upgraded independently.

Guiding principle: **the gateway is the only entry point**. CUPS and the printer have no published ports, and the user
name CUPS sees never comes from the client: it is decided by the gateway after authentication.

```
                         ┌──────────────────────────────────────────┐
  IPP client  ──ipps──▶  │ GATEWAY (container, port 8631)           │
  (PC, Mac, …)  TLS +    │  • TLS + HTTP Basic (name + print key)   │ ──▶ RADIUS (optional)
  Basic auth             │  • identity imposed by the gateway       │
                         │  • quotas / grants + audit  ◀──▶  DB     │
  browser  ───https────▶ │  • portal: /login /admin /me /docs       │
                         └──────────────┬───────────────────────────┘
                                        │ unix socket (shared volume)
                         ┌──────────────▼───────────────┐
                         │ CUPS 2.4 (container)         │  no TCP port
                         └──────────────┬───────────────┘
                                        │ internal docker network
                         ┌──────────────▼───────────────┐
                         │ ipp-usb (container)          │  /dev/bus/usb
                         └──────────────┬───────────────┘
                                        │ USB
                                     printer / scanner

   mdns (container, host network): reads the queue list and announces it on the LAN (DNS-SD / AirPrint / Mopria)
```

Deliberate differences from a "classic" design:

- the policy engine and the database live **inside** the gateway, they are not separate services;
- scanning goes through **eSCL** (ipp-usb already serves it on the same port as IPP), not through SANE;
- the IPP gateway is a real **reverse proxy that decodes IPP**, not a CUPS filter or backend (those would not receive a
  trustworthy identity).

## 2. Technologies and why they were chosen

| Component | Choice | Why |
|---|---|---|
| Language | **Python 3.12+** | Easy to debug, many libraries; the IPP codec is isolated and replaceable |
| Web / proxy | **FastAPI + uvicorn**, **httpx** (async, also over a unix socket) | Streams documents without loading them in memory |
| Database | **SQLAlchemy 2** + **SQLite** (WAL), **Alembic** migrations | Zero configuration; a single file, no server to manage |
| Local passwords | **argon2id** (`argon2-cffi`) | Human-chosen secrets: slow hash (32 MiB per check; at most `SPOOLSTEWARD_PASSWORD_CHECKS_AT_ONCE` run at the same time, the others wait) |
| Print keys | 192 random bits, **sha256** | High entropy: a fast hash is enough and does not slow down each IPP request |
| RADIUS | **pyrad** (PAP + Message-Authenticator) | The office already has a RADIUS server |
| Portal | **Jinja2** server-rendered pages, signed sessions (**itsdangerous**) | HTML from the server; JavaScript only for small helpers (copy, hints, popups, auto-refresh) |
| Translations | **Babel** (gettext `.po` catalogs read in memory) | No build step; see [languages.md](languages.md) |
| Print backend, USB bridge | **CUPS 2.4**, **ipp-usb** | Unmodified dependencies; filters and drivers already exist; ipp-usb exposes the USB printer as IPP/eSCL |
| Page counting | **pypdf** (+ image headers, URF, PostScript) | The page limit must hold for a 200-page file too |
| Documentation, report PDF | **markdown-it-py**, **reportlab** | The Markdown pages of `docs/` render with raw HTML off and stay readable on GitHub; PDF tables need no browser |
| Backups | **cryptography** (X25519, HKDF, AES-256-GCM, scrypt) | Encrypted with a recovery key, passphrase never stored |
| LAN announcement | **python-zeroconf** (`mdns` service, host network) | Multicast does not cross docker's bridge network |
| Containers, tests | **Docker Compose**, **pytest** | Nothing installed on the host; many tests run against a real CUPS and FreeRADIUS |

## 3. The containers

There are two images. `spoolsteward` holds the application, CUPS and ipp-usb: the services `ipp-usb`, `cups`, `gateway` and `mdns` all
start it, each with its own command, so they always have the same version. `spoolsteward-updater` is the optional updater.

| Service | Role | Exposure |
|---|---|---|
| `ipp-usb` | Only owner of `/dev/bus/usb`; turns each USB device into an IPP + eSCL endpoint. A script restarts it when the USB device list changes (udev events do not reach containers). | none (internal network) |
| `cups` | Print backend. Queues are created at start from `CUPS_QUEUES` and, for USB printers, by the gateway's discovery; configuration is persistent; unix socket only. It sees `/dev/bus/usb` for printers ipp-usb can not serve (it needs a driver). | none |
| `gateway` | The application: IPP proxy, eSCL proxy, portal, schedulers. Non-root user (`PUID:PGID`), read-only filesystem, no capabilities. | **port 8631** (the only one) |
| `mdns` | Announces the queues and scanners on the LAN. Same image as the portal, host network. Asks the gateway what to announce (authenticated with a code derived from `SPOOLSTEWARD_SECRET_KEY`). | multicast |
| `radius` | Test RADIUS, only with `--profile radius-test` | internal |

Data: a docker volume for the database (`/data`), host folders `data/audit-archive` and `data/backups`, `config/` (read-only) for the TLS certificate, and `.env` (read-only, for encrypted backups).

## 4. Path of an IPP request

1. Only `application/ipp`, only HTTPS.
2. **Capability queries** (`Get-Printer-Attributes` and `Validate-Job`, declared body ≤ 64 KB, `SPOOLSTEWARD_ANONYMOUS_DISCOVERY`) are answered
   without credentials: clients ask them before they have a password. They create no users and no audit rows, and carry no document.
   `Validate-Job` is not checked against the quota when anonymous.
3. **Authentication**: HTTP Basic, user name + **print key**. The domain password is not accepted
   (`SPOOLSTEWARD_IPP_AUTH=keys`); `keys+radius` re-admits it for RADIUS users during a transition, never for local accounts.
4. **Operation filter**: users may print, cancel, list jobs and printers; CUPS administrative operations and
   `Print-URI` / `Send-URI` (SSRF risk) are forbidden for everyone.
5. **Identity**: `requesting-user-name` is overwritten with `gw:<user>`. The prefix prevents a name such as `root` from
   matching a CUPS administrator.
6. **Consistency**: the `printer-uri` in the body must match the HTTP path.
7. **Blocks and maintenance**: a blocked printer or the gateway maintenance mode refuses the job (see
   [printers.md](printers.md)).
8. **Permissions** (colour, one-sided, printer, hours): in `adjust` mode `print-color-mode` / `sides` are rewritten before CUPS, otherwise
   the job is refused; printer lists are filtered ([quotas.md](quotas.md#5-advanced-permissions)). The emergency administrator is exempt.
9. **Quota**: atomic reservation of the slot ([quotas.md](quotas.md)); if exceeded, an IPP error with the reason **and the
   portal address** to ask for an exception.
10. **Forwarding** to CUPS in streaming (size cap `SPOOLSTEWARD_MAX_REQUEST_MB`, default 200); internal URIs are rewritten,
   `device-uri` hidden, CUPS HTML refusals turned into clean IPP errors.
11. **Recording** of the outcome; if the upload is interrupted the row becomes `failed` and the slot is freed.
12. Later, the **reconciler** replaces the page estimate with the real pages printed and frees quota for jobs that never printed. A CUPS job is matched to its audit row by job number *and* `time-at-creation` (CUPS restarts its numbering if its volume is recreated).

Printing from the browser (`/me/print`) goes through the same checks (quota, audit, identity `gw:<user>`), only the
authentication is the portal session.

## 5. Background tasks

All started by the gateway's lifespan ([`spoolsteward/main.py`](../../spoolsteward/main.py)):

| Task | Interval | What it does |
|---|---|---|
| Reconciler | `SPOOLSTEWARD_RECONCILE_INTERVAL` (30 s) | Syncs `job_log` with CUPS: real pages, quota back for canceled jobs, orphan reservations released after 1 h |
| Audit archiver | `SPOOLSTEWARD_AUDIT_CHECK_INTERVAL` (1 h) | Moves old audit rows to verified compressed files when the tables outgrow `SPOOLSTEWARD_AUDIT_MAX_MB` |
| Backup scheduler | 60 s | Runs the backup targets that are due |
| Keep-alive scheduler | 60 s | Prints maintenance pages and notes the first time each queue is seen |
| Printer discovery | `SPOOLSTEWARD_DISCOVERY_INTERVAL` (30 s) | Creates queues for new USB printers (ipp-usb first, CUPS usb backend second) and removes USB queues unreachable for `SPOOLSTEWARD_OFFLINE_REMOVE_HOURS` |
| mDNS announcer | `SPOOLSTEWARD_MDNS_INTERVAL` (30 s) | Separate container: re-reads the queue list and tells the gateway which address it announces |
| File names ([`spoolsteward/titles.py`](../../spoolsteward/titles.py)) | at start, then 1 day | When the encryption of names is on, encrypts the ones written in clear; with retention on, erases names older than `SPOOLSTEWARD_JOB_TITLE_RETENTION_DAYS` |
| Certificate check ([`spoolsteward/certwatch.py`](../../spoolsteward/certwatch.py)) | 6 h | Reads the TLS certificate and `config/tls/sync-status.json`, and opens or closes the certificate notices |
| Printer watch ([`spoolsteward/printerwatch.py`](../../spoolsteward/printerwatch.py)) | 1 min | Paper jam, paper out, ink out/low as notices (confirmed by two consecutive checks) |
| Key housekeeping ([`spoolsteward/keywatch.py`](../../spoolsteward/keywatch.py)) | 1 h | Removes finished print keys, warns about keys about to expire and about temporary permissions that ended |
| Update check ([`spoolsteward/updates.py`](../../spoolsteward/updates.py)) | 24 h (first 2 min after start) | Asks GitHub for the newest release (`SPOOLSTEWARD_UPDATE_CHECK`) |

## 6. Data model

Tables (Alembic applies migrations at start; a test fails if a model changes without a migration):

| Table | Content |
|---|---|
| `user`, `group` | People and groups. Every user has a `provider` (`local` or a delegated one such as RADIUS), a group, a theme and a language |
| `policy_rule` | Limit for a user or group: resource (print/scan), metric (**jobs** or **pages**), amount, period (weekly / monthly) |
| `job_log` | Audit of prints and scans: user, device, file name, outcome, pages, reason, options |
| `print_key` | Personal print keys (hash only), expiry, revocation, last use |
| `quota_request`, `quota_grant` | Requests from users (quota, group, permission) and the quota grants that approvals produce |
| `permission_rule`, `permission_grant` | Colour / one-sided / printers / hours of a group or user, and the permissions added to one user after a request (permanent or until a date) |
| `admin_audit` | Who did what in the portal |
| `audit_archive` | Registry of compressed audit files (path, range, rows, sha256) |
| `printer_control`, `printer_keepalive`, `printer_seen`, `printer_order`, `printer_offline`, `printer_former` | Per-queue state: block and mDNS hiding, maintenance print settings, first time seen, each administrator's order, since when a USB queue does not answer, former names |
| `notice` | The bell: notices for administrators and for single users |
| `gateway_setting` | Maintenance mode, instance id, file-name saving switch, secret-key fingerprint |
| `backup_target` | Backup destinations, one per administrator or the general one |

## 7. Design decisions worth knowing

- **User names are unique across all sources.** CUPS and the audit only receive `gw:<name>`; two people with the same
  name would share identity and quota. The *row* decides the login method: a local account can never be opened by RADIUS.
- **Print keys, not domain passwords, on client devices.** Operating systems store print credentials in the clear. A key is
  random, per device, revocable and valid only for printing; it never opens the portal.
- **Grants add, they never rewrite history.** `effective limit = rule + active grants`, computed inside the same locked
  check that reserves the quota. `job_log` is never edited to give someone more room.
- **Audit is append-only and kept forever.** Nothing in the portal deletes rows or archives; only the archiver moves rows,
  and only after the compressed copy has been read back and verified.
- **Atomic reservation.** The limit check and the insert happen under a lock (thread + `BEGIN IMMEDIATE` on SQLite), so concurrent jobs cannot overshoot the limit.
- **Nothing is assumed about the hardware.** Printers, colour, ink, counters, scanners and paper sizes are read from the devices at run time.
- **Fail closed.** RADIUS unreachable ⇒ access denied; no TLS ⇒ the gateway refuses to start; half-configured RADIUS ⇒ refuses to start.
- **English is the source language** of every visible text; other languages are catalogs ([languages.md](languages.md)).

## 8. Security summary

- CUPS is not reachable from the network or from other local users; the CUPS user is `gw:<user>`, so no RADIUS name
  (for example `root`) can become a CUPS administrator. IPP administrative operations are blocked.
- TLS is mandatory. Credentials never travel in clear over HTTP.
- Keys are stored only as hashes, shown once, and never open the portal; local passwords are argon2id (minimum 12 characters).
- Login lock-out: 5 *different* wrong passwords in 5 minutes per (user, IP), plus a per-IP cap; a stranger cannot lock another user out.
- Portal: signed 8-hour sessions, CSRF token on every POST, `Secure` + `SameSite=strict` cookies, escaped HTML,
  CSV protected against formula injection, secret-bearing pages sent with `Cache-Control: no-store`.
- The database holds printed file names: protect it as personal data.

## 9. Quality

Automated tests cover the IPP codec, policy windows (also across DST), concurrency, authentication against a real RADIUS, the gateway against a real CUPS,
the portal in every language, migrations, keys, requests, grants and archiving.
