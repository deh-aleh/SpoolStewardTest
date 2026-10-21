<picture>
  <source media="(prefers-color-scheme: dark)" srcset="spoolsteward/admin/static/icon-light.svg">
  <img src="spoolsteward/admin/static/icon-dark.svg" alt="SpoolSteward icon" width="96" align="right">
</picture>

# SpoolSteward

**English** · [Italiano](README.it.md)

> [!WARNING]
> **SpoolSteward is in beta (versions 0.x).** Until version 1.0 an update may contain **breaking changes**: settings in `.env`
> renamed or removed, database changes that an older version cannot read, a different install or update procedure. Each release
> lists them in its notes on GitHub. Read the notes before updating, keep a backup (`scripts/update.sh` makes one before every
> update) and do not use a beta on a server people depend on without being ready to restore it.

A printer on a shared network lets anyone print as much as they like, and nobody can tell afterwards who printed what. SpoolSteward sits between people and the printer: everyone signs in, each person or group gets a limit (pages, colour, one-sided), and every print and scan is written down. It is for homes, offices and schools that share a printer and want to decide who prints how much, and to see what was printed.

Technically it is a gateway in front of CUPS (IPP) with a web portal. It signs people in (local accounts and/or RADIUS), decides the
user name CUPS sees, applies the limits, records who printed and scanned what, and forwards the job to CUPS. CUPS and ipp-usb are used
unmodified, and the gateway is the only way in.

```
IPP client ──ipps (TLS + Basic)──▶ gateway ──unix socket──▶ CUPS ──internal net──▶ ipp-usb ──USB──▶ printer
browser ───────https───────────▶     │  └─▶ DB (SQLite): users, groups, rules, audit
                                     └────▶ RADIUS (optional)           portal: /login, /admin (admins), /me (users)
```

## What it does

- **Accounts**: local (argon2id) and/or RADIUS; personal, revocable **print keys** so a domain password is never stored on a PC or phone.
- **Quotas**: jobs and pages per week or month, per group with per-user overrides; users ask for more, administrators approve.
- **Permissions**: colour, one-sided printing, which printers and at what hours, per group with per-user overrides; users ask for what they lack, for good or until a date.
- **Printing**: IPP/IPPS for computers, **print from the browser** for any phone, LAN announcement (AirPrint/Mopria).
- **Scanning**: eSCL through the gateway and from the portal, together with printing on the same USB device.
- **Audit**: who printed/scanned what (real pages), a separate log of administrative actions, kept forever with verified compressed archives.
- **Printers**: status, ink and counters, block/hide, test printers, periodic maintenance print and test print for physical printers.
- **Backups**: scheduled per administrator, encrypted with a recovery key, restorable from the command line.
- **Portal in 5 languages**: English, Italian, Spanish, Russian, Simplified Chinese; chosen per user. The documentation (English and Italian) is readable inside the portal by administrators.

## Features and status

✅ done and tested · 🟡 partial · ⬜ planned

| Area | Feature | Status |
|---|---|---|
| **Identity** | Local accounts (argon2id); RADIUS login (optional); first administrator through a one-time code | ✅ |
| | Other providers (LDAP/AD, OIDC); two-factor authentication for administrators | ⬜ |
| **Printing** | IPP/IPPS proxy in front of CUPS; no bypass (CUPS is not reachable); personal print keys | ✅ |
| | Any printer CUPS supports (USB through ipp-usb, or network) | 🟡 tested with two printers over USB: an HP Smart Tank 7300 (through ipp-usb) and a Brother DCP-7055 (CUPS usb backend, brlaser driver: found, queue created and first print worked without any setting) |
| | LAN announcement (DNS-SD) and printing from the browser | ✅ seen by Android with Mopria; iPhone not tested |
| | New USB printers get their queue by themselves (ipp-usb first, CUPS usb backend + driver otherwise); unreachable USB queues are removed after a countdown, any dead queue can be deleted by hand | ✅ |
| | Held printing with later release (pull printing) | ⬜ |
| **Policy** | Users and groups; job and page limits per week/month; per-user overrides | ✅ |
| | Exceptions: user requests, administrator approval, temporary or permanent grants; group change on request | ✅ |
| | Pages counted before printing for PDF, images, text and PostScript | ✅ soft limit for other files |
| | Permissions per group/user: colour, one-sided, printers, allowed hours; `adjust` or `refuse` mode; users ask for what they lack | 🟡 covered by tests, not yet checked on many real printers |
| **Scanning** | eSCL through the gateway (same keys, audit, together with printing), scanning from the portal, and a scan window (last resort: it trusts the TCP address, which NAT and reverse proxies break) so apps that cannot send a password can scan from the IP of whoever pressed a button | ✅ |
| | Native eSCL clients that can authenticate (field test) | ⬜ |
| **Audit** | Print/scan audit with real pages; separate log of administrative actions | ✅ |
| | Unlimited retention with verified compressed archives; CSV export, filters, archive viewer and download | ✅ |
| | Usage report: top users in a period, optional detail per printer, groups left out, page / CSV / PDF | ✅ |
| | File names encrypted in the database; button to stop saving them; optional erasing after some days | ✅ |
| | syslog/SIEM export; anonymisation | ⬜ |
| **Portal** | Admin portal (audit, users, groups, requests with grants, actions, printers, backups, archive) and user area (consumption, keys, requests, print, scan); the documentation is built in for administrators (English and Italian) | ✅ |
| | Settings: password, theme, language, refresh; phone-friendly layout; copy buttons | ✅ |
| | Printers page: status, ink, lifetime counters (HP), firmware, scanner; block, hide, test printers; gateway maintenance mode | ✅ |
| | Periodic maintenance print and test print with statistics (physical printers); ordering physical first, movable with up/down arrows (own order per administrator); queues can be renamed; IPP-USB / USB badges | ✅ |
| | Portal in English, Italian, Spanish, Russian, Chinese | ✅ Spanish/Russian/Chinese need native review |
| | Notifications (email/webhook) for pending requests | ⬜ |
| | Permission requests (colour, one-sided, printers, hours) | ✅ |
| **Operations** | Docker stack (ipp-usb + CUPS + portal + mDNS, from one image), nothing installed on the host | ✅ |
| | Scheduled backups per administrator (folder, hour, rotation, passphrase/recovery-key encryption), restore from CLI | ✅ |
| | Database migrations (Alembic), health check, logs, admin CLI | ✅ |
| | Images for amd64 and arm64 on ghcr.io; update script that downloads a checked release package, makes a backup first and goes back by itself (and offers to restore the backup) if the new version does not start; notice of new versions; one `.env` file that an update never touches | ✅ amd64; arm64 run on a Raspberry Pi 4B (64-bit) |
| | Update button in the portal (optional `updater` container, needs the Docker socket) | ✅ |
| | Backups on a network disk or USB disk: marker file against an unmounted disk (on by default), a notice in the bell when a backup fails, time limit for a disk that stops answering | ✅ tested with an SMB share |
| | TLS certificate: notices (expired, announced address missing, optional expiry warning) and `scripts/cert-sync.sh` to copy a renewed certificate from Nginx Proxy Manager or certbot | 🟡 script tested with simulations, not yet with a real Nginx Proxy Manager |
| | Bell with notices for administrators (one bell, their own notices first); printer problems (jam, paper, ink) for administrators and for the users waiting; users are told when their group, rights, quota or request change; a one-time reminder to save `SPOOLSTEWARD_SECRET_KEY` and a notice if it changes; `/healthz` shows only the status unless you ask for more | ✅ |

## Limits

The full list, with workarounds, is in [docs/en/limitations.md](docs/en/limitations.md). The ones to know before you start:

- **The pages limit is soft** for files whose pages cannot be counted (unknown format, encrypted PDF, over 64 MB): the job that
  crosses the limit *is printed* and its real pages then block the following ones. With `Create-Job` + `Send-Document` the count
  arrives after the job was accepted. A `Create-Job` without a document frees its quota only when CUPS cancels it (default 3 h).
- **Linux host required** for USB printers and mDNS announcement (Docker Desktop on Windows/macOS: network printers only, no discovery).
- **Android's system print service cannot authenticate**: use Mopria Print Service or print from the browser. Windows 11 25H2 is tested ([windows.md](docs/en/windows.md)); iPhone and macOS are untested.
- **One gateway per USB printer** and **one per SQLite database** (no high availability). Going back to an older version after a migration may need a backup.
- **TLS is mandatory**: install the self-signed certificate on clients or use your CA's.
- Printer disconnection is detected within ~30 s (network queues: a TCP check, not paper).
- Printed **file names are encrypted** in the database (key from `SPOOLSTEWARD_SECRET_KEY`: do not change it); the rest of the database is not. For everything else encrypt the disk: [data-protection.md](docs/en/data-protection.md).
- Spanish, Russian and Chinese translations are not reviewed by native speakers; pages printed by the gateway stay English.


## How it was developed

SpoolSteward started as a solution to a problem of my own, and I published it because it may be useful to others. I used AI tools in a significant way, and I would rather say so plainly:

- **Documentation, automated tests and code comments** were written mostly with AI assistance.
- **The application code** was written partly by me and partly with AI assistance.
- **Translations** were made with DeepL and then harmonised with AI tools. The Spanish, Russian and Chinese ones have not been reviewed by native speakers.

If you are looking for software written entirely by a human, this is not the right repository for you.

What you can check for yourself: the automated tests (`tests/`), the feature status table (what has been tried on real hardware) and the list of known limits. The code has not had an independent review: if you use it where it matters, look closely at the security-sensitive parts (authentication, print keys, certificates).


## Install (Docker)

Requirements: a **Linux** server, Docker Engine with Compose, `openssl`, `curl`, `tar`, `python3`, a USB or network printer, and a TCP port
reachable by clients (default 8631). Nothing is installed on the host.

```sh
# 0. download the release package (newest version: https://github.com/deh-aleh/SpoolSteward/releases)
VERSION=0.0.0-beta.1
curl -LO https://github.com/deh-aleh/SpoolSteward/releases/download/v$VERSION/spoolsteward-$VERSION.tar.gz
tar xzf spoolsteward-$VERSION.tar.gz && cd spoolsteward-$VERSION      # (the guide also shows how to check the download)

# 1. names / IPs clients will use to reach the server (they go into the TLS certificate)
scripts/docker-init.sh print.lan 192.168.1.50      # creates .env (all the settings), config/tls/, data/

# 2. settings: RADIUS is optional (write none in host and secret for local accounts only)
$EDITOR .env

# 3. printers: USB ones get their queue by themselves (stop the host's ipp-usb first, see the guide);
#    a network printer goes in .env:  CUPS_QUEUES="OFFICE=ipp://192.168.1.60/ipp/print"
docker compose up -d

# 4. read the one-time code and set the password of the administrator "administrator"
docker compose logs gateway | grep "FIRST RUN"
#    then open https://<server>:8631/setup
```

Full guide, with certificates, printers, updates and a test setup without printer or RADIUS: [docs/en/installation.md](docs/en/installation.md).
Then users and groups: [admin-guide.md](docs/en/admin-guide.md); connecting computers and phones: [printing.md](docs/en/printing.md).

## Documentation

| Topic | Document |
|---|---|
| **Installation** (Docker, printers, TLS, updates) | [installation.md](docs/en/installation.md) |
| **Troubleshooting** | [troubleshooting.md](docs/en/troubleshooting.md) |
| Architecture and technologies | [architecture.md](docs/en/architecture.md) |
| How the application is built (each source file, `.env`, scripts) | [application-internals.md](docs/en/application-internals.md) |
| User guide · Administrator guide (incl. CLI) | [user-guide.md](docs/en/user-guide.md) · [admin-guide.md](docs/en/admin-guide.md) |
| Accounts, groups, print keys | [accounts.md](docs/en/accounts.md) |
| Quotas, requests, grants | [quotas.md](docs/en/quotas.md) |
| Printing and connecting devices | [printing.md](docs/en/printing.md) |
| Printers: overview, controls, maintenance print | [printers.md](docs/en/printers.md) |
| Scanning | [scanning.md](docs/en/scanning.md) |
| Audit and archive | [audit.md](docs/en/audit.md) |
| Backup and restore | [backup.md](docs/en/backup.md) |
| TLS certificates: notices, CA file, automatic synchronisation | [certificates.md](docs/en/certificates.md) |
| Data protection: file names, encryption, retention | [data-protection.md](docs/en/data-protection.md) |
| Languages and translating | [languages.md](docs/en/languages.md) |
| Known limitations | [limitations.md](docs/en/limitations.md) |

Everything is also available in Italian: [docs/it](docs/it) · [docs/README.md](docs/README.md).

## License

SpoolSteward is released under the [Apache License 2.0](LICENSE). You may use, modify and distribute it, commercially included, as long as you keep
the copyright notice and the [NOTICE](NOTICE) file (credit to Alessio Paccino) and state the changes you make. Copyright 2026 Alessio Paccino.
