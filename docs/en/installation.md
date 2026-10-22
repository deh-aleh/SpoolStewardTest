# Installation guide

[Documentation index](../README.md) · English · [Italiano](../it/installazione.md)

SpoolSteward runs in Docker containers (`ipp-usb`, CUPS, gateway, mDNS): nothing is installed or changed on the host. This guide installs
it from the release package with the published images. Building the images from source is for developers: [CONTRIBUTING.md](../../CONTRIBUTING.md).
Problems: [troubleshooting.md](troubleshooting.md).

## 1. Requirements

| | |
|---|---|
| Server | **Linux** machine (a mini-PC or a Raspberry Pi is ideal). USB passthrough and the `host` network used by mDNS exist only on Linux; on Docker Desktop (Windows, macOS) the gateway starts but only **network** printers work and phones will not discover them. |
| Software | Docker Engine with the Compose plugin (`docker compose version`), `openssl`, `curl` and `tar` (used by the scripts), `python3` (used by `scripts/backup.sh`). `git` is not needed. |
| Printer | USB (through `ipp-usb`) or on the network (IPP). Anything CUPS can drive. |
| Network | One TCP port reachable by clients (default **8631**; `631` if no other CUPS uses it). Clients must trust the certificate (§3.3). |
| Architecture | **amd64** (PC, server) and **arm64** (Raspberry Pi 4/5, ARM NAS). The published images contain both and Docker picks the right one. 32-bit ARM (armv7) is not supported. |
| Memory | About **550 MB** of free RAM for the whole stack, Docker itself included. Measured at rest on a Raspberry Pi 4 (arm64) and on an amd64 machine: roughly 400 MB on both; the rest is a margin for printing and scanning. A job with a large document needs more for a moment. Measure your own machine with `scripts/resources.sh` (CPU and RAM of the containers and of Docker, added up; `watch -n 5 scripts/resources.sh` to follow it). |
| One gateway per USB printer | `ipp-usb` needs exclusive use of the device (§3.4). |

## 2. Quick path

```sh
# 0. download the package of the newest release (https://github.com/deh-aleh/SpoolSteward/releases) and check it
VERSION=w.x.y #w.x.y-beta.z if you want to install a beta version
curl -LO https://github.com/deh-aleh/SpoolSteward/releases/download/v$VERSION/spoolsteward-$VERSION.tar.gz
curl -LO https://github.com/deh-aleh/SpoolSteward/releases/download/v$VERSION/SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS            # must answer "OK"
tar xzf spoolsteward-$VERSION.tar.gz && cd spoolsteward-$VERSION

# 1. names / IPs by which clients will reach the server (they go into the TLS certificate)
scripts/docker-init.sh print.lan 192.168.1.50
#    it must print "created .env": if it prints "kept the existing .env", this folder already had one (§3.1)

# 2. settings (RADIUS is optional: write none in host and secret to use local accounts only)
$EDITOR .env

# 3. printers (§3.4), then start
docker compose up -d

# 4. read the one-time code and choose the administrator password
docker compose logs gateway | grep "FIRST RUN"
#    open https://<server>:8631/setup
```

## 3. Step by step

### 3.1 Initialise

`scripts/docker-init.sh [name-or-IP ...]` writes **only inside the project folder**:

| Created | What |
|---|---|
| `.env` | **all** the settings in one file, made from `docker/env.example` with your user (`PUID`/`PGID`), this folder, the version and a freshly generated `SPOOLSTEWARD_SECRET_KEY`. Mode 600 (it holds secrets). |
| `config/tls/` | a self-signed certificate (825 days) whose alternative names are the names/IPs you passed |
| `data/audit-archive/`, `data/backups/` | host folders for compressed audit and backups |

Running it again never replaces an existing file. **An existing `.env` is never modified**, not even to add a missing line: the script
prints "kept the existing .env". If that file has no `SPOOLSTEWARD_SECRET_KEY=` line, or the key is `none`, still `__GENERATED__` or
shorter than 64 characters, it also prints a warning; what to do: [troubleshooting.md](troubleshooting.md#no-secret-key-is-set).

Do not copy `docker/env.example` to `.env` by hand: the script fills in your user, this folder, the version and the key.

After the first start the portal reminds the first administrator, once, to copy `SPOOLSTEWARD_SECRET_KEY` to a safe place
([backup.md](backup.md#save-the-secret-key)). Never share `config/` or `.env`: they hold secrets.

**Never edit `docker-compose.yml`**: everything you may change is in `.env`, so an update can replace the compose file without erasing
your choices.

### 3.2 Settings

`.env` is fully commented. At install time look at `SPOOLSTEWARD_TIMEZONE` (default `UTC`), `SPOOLSTEWARD_RADIUS_HOST` and
`SPOOLSTEWARD_RADIUS_SECRET` (both `none` for local accounts only), `SPOOLSTEWARD_PORTAL_URL` and `PORTAL_PORT`. Every setting, with its
default and its rules: [configuration.md](configuration.md). After any change: `docker compose up -d`.

### 3.3 TLS certificate

TLS is mandatory: the gateway refuses to start without a certificate. The one made by `docker-init.sh` is self-signed: install
`config/tls/fullchain.pem` as trusted on every client, or replace the pair in `config/tls/` with a certificate from your CA. The
certificate must list the names/IPs clients use, including the address announced by mDNS. How to install the CA on each device, use a
certificate you already have, keep it renewed, or work behind a reverse proxy: [certificates.md](certificates.md).

### 3.4 Printers

**USB.** `ipp-usb` needs exclusive use of the printer. If it is installed on the host, stop and mask it (SANE too):

```sh
sudo systemctl disable --now ipp-usb && sudo systemctl mask ipp-usb
```

A USB printer gets its queue by itself within about a minute of being plugged in; nothing has to be written in `.env`. How printers are
found, named and removed: [printers.md](printers.md#2-new-and-unplugged-usb-printers).

**Network.** Give `CUPS_QUEUES` in `.env` the printer's IPP address: `CUPS_QUEUES="OFFICE=ipp://192.168.1.60/ipp/print"` (several queues
separated by spaces). The rules of that line: [configuration.md](configuration.md#11-part-a-docker-compose-variables). Scanners:
[scanning.md](scanning.md).

### 3.5 Start

```sh
docker compose up -d
```

The first start downloads the images (`ghcr.io/deh-aleh/spoolsteward:<version>`). Check: `curl -sk https://<server>:8631/healthz` answers
200 and `docker compose ps` shows `cups` healthy.

### 3.6 First administrator

`docker compose logs gateway | grep "FIRST RUN"` prints a one-time code. Open `https://<server>:8631/setup`, enter the code and choose the
password of `administrator` (minimum 12 characters). Details: [accounts.md](accounts.md#2-the-first-administrator). If nothing is printed:
[troubleshooting.md](troubleshooting.md#2-cannot-sign-in).

### 3.7 Users and devices

Create users and groups in the portal: [admin-guide.md](admin-guide.md). Connecting computers and phones: [printing.md](printing.md),
Windows step by step: [windows.md](windows.md). What users do: [user-guide.md](user-guide.md).

## 4. Try it without a printer or RADIUS

```sh
# in .env:  CUPS_VIRTUAL_QUEUE=1   SPOOLSTEWARD_RADIUS_HOST=radius   SPOOLSTEWARD_RADIUS_SECRET=testing123
docker compose --profile radius-test up -d
```

Creates the queue `virtual` (writes PDFs, no paper) and a test RADIUS with `mario/mario123`, `anna/anna123`, `admin/admin123`. Example:
`ipptool -t -f file.txt "ipps://mario:mario123@localhost:8631/printers/virtual" job.test`. PDFs:
`docker compose exec cups ls /var/spool/cups-pdf/ANONYMOUS`. Never leave test accounts on a real installation.

## 5. Updates

Each release publishes the images (`ghcr.io/deh-aleh/spoolsteward:<version>`, plus `spoolsteward-updater`; amd64 + arm64) and a
**package** (`spoolsteward-<version>.tar.gz` with its `SHA256SUMS`) holding `docker-compose.yml`, the scripts, `docker/env.example` and
the documentation. The package never holds `.env`, `config/` or `data/`: an update cannot touch them. `.env` holds the version that runs
(`SPOOLSTEWARD_VERSION`); it changes only when you update, never by itself. Database migrations run at start.

**`scripts/update.sh`** is the normal way. It makes a full backup (`data/backups/pre-update/`), downloads the package of the new version
and checks its sha256 sum, replaces `docker-compose.yml` and the scripts (the old ones stay in `.update-prev/`), writes the new version in
`.env`, downloads the images, restarts and waits until the gateway is healthy. If it is not, it puts the previous files and version back;
if even the previous version does not start, it offers to restore the backup it has just made. `scripts/update.sh <version>` picks a
version; `--check` only tells whether a newer one exists.

**The *Update* button in the portal** (menu *Backup*, box *Update*) is optional. It needs the `updater` container (below) and a scheduled
backup that is on and succeeded in the last 48 hours: a fresh backup is made, then the update starts. If the new version does not start
the old one is put back; restoring the backup, if needed, is up to you with `scripts/backup.sh restore`.

**Betas.** A version such as `0.0.0-beta.2` is a test release, published as a GitHub pre-release. With
`SPOOLSTEWARD_UPDATE_BETA=true` (the default written by `docker-init.sh`) betas are offered like final releases: in the note of a new
version, by the *Update* button and by `scripts/update.sh` without arguments. Installing a beta asks for one more confirmation (a box
to tick in the portal; `update.sh` asks you to type `YES`). With `false` only final releases are offered; a beta you name
(`scripts/update.sh 0.0.0-beta.2`) is installed whatever the setting says.

**Breaking changes.** A release may declare breaking changes: its release notes have a "Breaking changes" heading, or the first number of
the version changes, or the second one while the first is `0` (`0.1.x` to `0.2.0`). A release you skip counts too: going from
`0.0.0-beta.1` to `0.0.0-beta.3` is breaking if `0.0.0-beta.2` was. Then the confirmation warns that the update could fail and leave
SpoolSteward not working, and asks you to copy a recent backup off the server and keep a copy of `.env` and `config/` first; the portal
starts the update only with the box ticked, `update.sh` only after `YES`. Read the release notes before you confirm.

**Major versions.** An update that changes the first number of the version (`1.x` to `2.0.0`) cannot be installed from the portal: the
*Update* box shows it with the button off. Run `scripts/update.sh` in a terminal, after reading the release notes.

**Notice of a new version.** Once a day the gateway asks GitHub for the list of releases of `SPOOLSTEWARD_UPDATE_REPOSITORY` and shows a
note to administrators. Only that request goes out and it sends nothing about your server. `SPOOLSTEWARD_UPDATE_CHECK=false` switches it off
([configuration.md](configuration.md#5-updates)).

**New settings.** An update never modifies `.env`. A new setting works with its default even if it is not in your file; `docker/env.example`
(replaced by every update) shows it with its explanation. To change one, copy its line into `.env` and run `docker compose up -d`.

**The optional `updater` container** applies the updates asked for in the portal. It is **off by default** because it needs the Docker
socket (`/var/run/docker.sock`), and whoever controls that socket controls the whole machine (root): someone who found a flaw in the
portal could use it to command Docker and take the server. It only reads a version number written by the gateway; images always come from
the registry in your `.env`. It keeps the version it was created with until you run `docker compose up -d` in a terminal. Turn it on with:

```sh
echo "COMPOSE_PROFILES=autoupdate" >> .env      # one line in .env (or add --profile autoupdate every time)
docker compose up -d
```

If you do not want that risk, keep it off and use `scripts/update.sh`. To turn it off again: remove the line from `.env` and run
`docker compose stop updater`.

**Going back to an older version.** `scripts/update.sh <version>` puts the files and images of that version back. It is done **only from a terminal**: the *Update* button of the portal goes forward only, and refuses an older version (the request comes from the gateway, the part of the system that faces the network, and must not be able to install a version with a known flaw). An older version
starts on a database that a newer one migrated only if those migrations kept it readable; when it does not start, or the new version
damaged something, use the backup that `update.sh` made **before** the update:

```sh
ls data/backups/pre-update/
#   spoolsteward-v<version>-<date>-<time>.tar.gz   <- one per update, named after the version that was running
scripts/backup.sh restore data/backups/pre-update/spoolsteward-v<version>-<date>-<time>.tar.gz    # asks you to type YES
scripts/update.sh <version>                # puts the files of that version back (the restore brought back its .env)
```

Everything done after that backup (print history, new users, requests) is lost; the backup also brings back the `.env` of that moment.
Each attempt makes a new backup first, so nothing is overwritten.

**Pin the version.** Never use `latest`: leave `SPOOLSTEWARD_VERSION` at a number, so an update is a decision.

## 6. Backups and moving to another machine

Scheduled backups from the portal and their restore: [backup.md](backup.md). `scripts/backup.sh backup` makes a full host-side copy
(database, `config/`, `.env`, audit archive, CUPS queues) to move the installation: [backup.md](backup.md#3-host-side-full-backup).
