<picture>
  <source media="(prefers-color-scheme: dark)" srcset="spoolsteward/admin/static/icon-light.svg">
  <img src="spoolsteward/admin/static/icon-dark.svg" alt="SpoolSteward icon" width="96" align="right">
</picture>

# SpoolSteward

**English** · [Italiano](README.it.md)

> [!NOTE]
> **Beta (0.x):** updates may contain breaking changes. Read the [warning before installing](#install-docker).

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

## Status

**Tested and working:** printing through the gateway (IPPS, print keys, printing from the browser), quotas and exceptions, permissions per group/user (colour, one-sided, printers, hours; checked on the two printers below), audit and archives, scanning (eSCL, portal, scan window), the printers page, scheduled backups (also to an SMB share), the Docker stack with the update script and the portal's update button (amd64; arm64 on a Raspberry Pi 4B), the notices bell.

**Only partly tested:**

- Printers: two over USB so far, an HP Smart Tank 7300 (through ipp-usb) and a Brother DCP-7055 (CUPS usb backend, brlaser driver: found, queue created and first print worked without any setting). Any other printer CUPS supports should work, but has not been tried; this includes the permissions, which were checked only on these two.
- LAN announcement (DNS-SD): seen by Android with Mopria; iPhone not tested.
- `scripts/cert-sync.sh`: tested with simulations, not yet with a real Nginx Proxy Manager.
- Spanish, Russian and Chinese translations: no native review.

**Not done yet:** two-factor authentication for administrators; held printing with later release (pull printing); native eSCL clients that can authenticate; email/webhook notifications for pending requests.

Details for each area are in the [documentation](#documentation).

## Limits

The full list is in [docs/en/limitations.md](docs/en/limitations.md). The ones to know before you start:

- **The pages limit is soft** for files whose pages cannot be counted (unknown format, encrypted PDF, over 64 MB): the job that crosses the limit *is printed* and its real pages then block the following ones.
- **Linux host required** for USB printers and mDNS announcement (Docker Desktop on Windows/macOS: network printers only, no discovery).
- **Android's system print service cannot authenticate**: use Mopria Print Service or print from the browser. Windows 11 25H2 is tested ([windows.md](docs/en/windows.md)); iPhone and macOS are untested.
- **One gateway per USB printer** and **one per SQLite database** (no high availability).
- **TLS is mandatory**: install the self-signed certificate on clients or use your CA's.
- Printed **file names are encrypted** in the database (key from `SPOOLSTEWARD_SECRET_KEY`: do not change it); the rest is not. See [data-protection.md](docs/en/data-protection.md).
- Spanish, Russian and Chinese translations are not reviewed by native speakers.

## Install (Docker)

> [!WARNING]
> **SpoolSteward is in beta (versions 0.x).** Until version 1.0 an update may contain **breaking changes**: settings in `.env`
> renamed or removed, database changes that an older version cannot read, a different install or update procedure. Each release
> lists them in its notes on GitHub. Read the notes before updating, keep a backup (`scripts/update.sh` makes one before every
> update) and do not use a beta on a server people depend on without being ready to restore it.

Requirements: a **Linux** server, Docker Engine with Compose, `openssl`, `curl`, `tar`, `python3`, a USB or network printer, and a TCP port
reachable by clients (default 8631). Nothing is installed on the host.

```sh
# 0. download the release package (newest version: https://github.com/deh-aleh/SpoolSteward/releases)
VERSION=w.x.y #w.x.y-beta.z if you want to install a beta version
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

## How it was developed

SpoolSteward started as a solution to a problem of my own, and I published it because it may be useful to others. I used AI tools in a significant way, and I would rather say so plainly:

- **Documentation, automated tests and code comments** were written mostly with AI assistance.
- **The application code** was written partly by me and partly with AI assistance.
- **Translations** were made with DeepL and then harmonised with AI tools. The Spanish, Russian and Chinese ones have not been reviewed by native speakers.

If you are looking for software written entirely by a human, this is not the right repository for you.

What you can check for yourself: the automated tests (`tests/`), the Status section (what has been tried on real hardware) and the list of known limits. The code has not had an independent review: if you use it where it matters, look closely at the security-sensitive parts (authentication, print keys, certificates).

## Documentation

| Topic | Document |
|---|---|
| **Installation** (Docker, printers, TLS, updates) | [installation.md](docs/en/installation.md) |
| **Troubleshooting** | [troubleshooting.md](docs/en/troubleshooting.md) |
| Installation reference: every `.env` setting | [configuration.md](docs/en/configuration.md) |
| Windows 11 step by step | [windows.md](docs/en/windows.md) |
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
