# Installation guide

[Documentation index](../README.md) · English · [Italiano](../it/installazione.md)

Two ways to install SpoolSteward:

- **Docker (recommended)** — everything runs in containers (`ipp-usb`, CUPS, gateway, mDNS). Nothing is installed or
  changed on the host.
- **Host install (advanced)** — a systemd service plus a reconfigured system CUPS. It modifies `/etc/cups/cupsd.conf`:
  use it only if you know why.

Problems? See [troubleshooting.md](troubleshooting.md).

## 1. Requirements

| | |
|---|---|
| Server | **Linux** machine (a mini-PC or a Raspberry Pi is ideal). USB passthrough and the `host` network used by mDNS exist only on Linux; on Docker Desktop (Windows, macOS) the gateway starts but only **network** printers work and phones will not discover them. |
| Software | Docker Engine with the Compose plugin (`docker compose version`), `openssl`, `curl` and `tar` (used by the scripts), `python3` (used by `scripts/backup.sh`). `git` is not needed: the install starts from the release package. |
| Printer | USB (through `ipp-usb`) or on the network (IPP). Anything CUPS can drive. |
| Network | One TCP port reachable by clients (default **8631**; `631` if no other CUPS uses it). Clients must trust the certificate (see §3.4) |
| Architecture | **amd64** (PC, server) and **arm64** (Raspberry Pi 4/5, ARM NAS). The published images contain both and Docker picks the right one. 32-bit ARM (armv7) is not supported |
| One gateway per USB printer | `ipp-usb` needs exclusive use of the device: see §5 |

## 2. Quick path (Docker)

```sh
# 0. download the package; VERSION = the newest release (https://github.com/deh-aleh/SpoolSteward/releases)
VERSION=x.y.z
curl -LO https://github.com/deh-aleh/SpoolSteward/releases/download/v$VERSION/spoolsteward-$VERSION.tar.gz
curl -LO https://github.com/deh-aleh/SpoolSteward/releases/download/v$VERSION/SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS            # must answer "OK"
tar xzf spoolsteward-$VERSION.tar.gz && cd spoolsteward-$VERSION

# 1. names / IPs by which clients will reach the server (they go into the TLS certificate)
scripts/docker-init.sh print.lan 192.168.1.50

# 2. settings (RADIUS is optional: write none in host and secret to use local accounts only)
$EDITOR .env

# 3. printers: see §5 (USB) and §6 (network); then start
docker compose up -d

# 4. read the one-time code and choose the administrator password
docker compose logs gateway | grep "FIRST RUN"
#    open https://<server>:8631/setup
```

The administrator account is `administrator`. From a shell you can do the same with
`docker compose exec -it gateway spoolsteward-admin set-admin-password`.

## 3. Step by step

### 3.1 Initialise

`scripts/docker-init.sh [name-or-IP ...]` writes **only inside the project folder**:

| Created | What |
|---|---|
| `.env` | **all** the settings in one file. Part A is for compose: `SPOOLSTEWARD_REGISTRY`/`SPOOLSTEWARD_VERSION` (which published images run), `PUID`/`PGID` (your user), `PROJECT_DIR`, `PORTAL_PORT`, `CUPS_QUEUES`, `CUPS_VIRTUAL_QUEUE`, host paths. Part B holds the settings of the program, `SPOOLSTEWARD_*`, with a freshly generated `SPOOLSTEWARD_SECRET_KEY`. Mode 600 (it holds secrets) |
| `config/tls/` | a self-signed certificate (825 days) whose alternative names are the names/IPs you passed |
| `data/audit-archive/`, `data/backups/` | host folders for compressed audit and backups |

Re-running it never overwrites existing files. Never share `config/` or `.env`: they contain secrets.

**Never edit `docker-compose.yml`**: everything you may change is in `.env`. That way an update can replace the compose file without erasing your choices.

### 3.2 The settings of the program (part B of `.env`)

The settings most often changed (all have a default):

| Setting | When |
|---|---|
| `SPOOLSTEWARD_RADIUS_HOST`, `SPOOLSTEWARD_RADIUS_SECRET` | You want office (RADIUS) logins. Register this machine as a RADIUS *client* with the same secret. Fill both or neither. |
| `SPOOLSTEWARD_PORTAL_URL` | Set `https://print.lan:8631` so messages sent to print clients contain the right link |
| `SPOOLSTEWARD_TIMEZONE` | Quota weeks/months and displayed times follow it (default `UTC`: set yours, for example `Europe/Rome`); the times in the logs (`docker compose logs`) follow it too and show the offset from UTC |
| `SPOOLSTEWARD_LANGUAGE` | Default portal language (`en`, `it`, `es`, `ru`, `zh`) |
| `SPOOLSTEWARD_MDNS_HOSTNAME` | If the machine name is not in the certificate |
| `SPOOLSTEWARD_BACKUP_*` | The general scheduled backup (see [backup.md](backup.md)) |
| `SPOOLSTEWARD_HEALTHZ_INFO` | What `/healthz` shows besides the status (nothing by default): `version`, `printers` |
| `SPOOLSTEWARD_UPDATE_CHECK` | `false` stops the daily question to GitHub about a newer version |
| `SPOOLSTEWARD_ESCL_ANONYMOUS` | `true` accepts scans from apps that cannot send a password (see [scanning.md](scanning.md)) |

The file is fully commented. Every variable, with its default and when to change it, is in [configuration.md](configuration.md). After any change: `docker compose up -d`.

### 3.3 The compose variables (part A of `.env`)

Also in `.env`, edit the printer queues (`CUPS_QUEUES`), the published port (`PORTAL_PORT=631` if no other CUPS uses it), the
user (`PUID`/`PGID`) and, for downloaded images, `SPOOLSTEWARD_REGISTRY` + `SPOOLSTEWARD_VERSION` (pin a fixed version in
production, never `latest`).

### 3.4 TLS certificate

TLS is **mandatory**: the gateway refuses to start without it.

- The generated certificate is self-signed. Install it as trusted on every client, **or**
- put your CA-signed `fullchain.pem` and `privkey.pem` in `config/tls/` (the key must be readable by the `PUID` user) and
  run `docker compose restart gateway`.

The certificate must list the names/IPs clients use, including the IP announced by mDNS. If it does not, the portal says so with a notice (the bell): see [certificates.md](certificates.md).

**You already have a valid certificate (for example one issued by Nginx Proxy Manager on another machine).** The simplest way is to let the
gateway use that same certificate itself, with no proxy in between:

1. Make a name that certificate covers (say `print.example.com`) resolve to the server inside your network (local DNS record or router entry).
2. Copy the pair from the machine that holds it (NPM: `data/letsencrypt/live/npm-<n>/fullchain.pem` and `privkey.pem`; custom certificates:
   `data/custom_ssl/npm-<n>/`) to `config/tls/fullchain.pem` and `config/tls/privkey.pem`, make the key readable by the `PUID` user
   (`chmod 600`), and run `docker compose restart gateway`.
3. Set `SPOOLSTEWARD_PORTAL_URL=https://print.example.com:8631` in `.env`, and use `ipps://print.example.com:8631/printers/<queue>`.
4. Let's Encrypt certificates are renewed about every 60 days. To avoid copying them by hand each time, run `scripts/cert-sync.sh` from cron: it copies the
   two files, checks them and restarts the gateway only if they changed. It is explained step by step, including how to create a user that can
   only read, in [certificates.md](certificates.md).

Devices that find printers through mDNS are given the `<name>.local` name or the IP, which a certificate for `print.example.com` does not
cover: use the real name by hand on those devices, or stop the `mdns` service if you do not need discovery.

*Behind a reverse proxy instead* (the proxy keeps the certificate and forwards to `https://<server>:8631`; not tested by the project): the
proxy host must forward to scheme `https`, allow large bodies and not buffer them (`client_max_body_size 250m; proxy_request_buffering off;
proxy_read_timeout 600s;`), and the gateway sees every client as the proxy's address (login lock-out per address, audit IPs) unless
`FORWARDED_ALLOW_IPS=<proxy address>` is set in `.env` so uvicorn trusts `X-Forwarded-For`. Printer addresses given back to clients
carry the `Host` the proxy forwards, without `:443`, so devices must keep the address they were set up with.

### 3.5 Start

```sh
docker compose up -d --build          # build from source
# or, with published images (set SPOOLSTEWARD_REGISTRY and SPOOLSTEWARD_VERSION in .env):
docker compose pull && docker compose up -d
```

Check: `curl -sk https://<server>:8631/healthz` answers 200; `docker compose ps` shows `cups` healthy.

### 3.6 First administrator

`docker compose logs gateway | grep "FIRST RUN"` prints a one-time code. Open `https://<server>:8631/setup`, enter
the code and choose the password of `administrator` (minimum 12 characters). The code is single-use and protected
against guessing. If nothing appears, the account probably already has a password (this folder ran before and the Docker volume kept the database): the log then says "already has a password"; sign in, or reset it with `docker compose exec -it gateway spoolsteward-admin set-admin-password`. More causes: [troubleshooting.md](troubleshooting.md).

### 3.7 First users

In the portal (**Users**) create local users, or let RADIUS users log in (they are created in the default group on first
login). In **Groups** set limits. Each user then creates a **print key** in *Profile* and uses it as the password
when a computer or phone asks for credentials. See [user-guide.md](user-guide.md) and [admin-guide.md](admin-guide.md).

## 4. Connecting devices

| Device | How |
|---|---|
| Any phone / computer | Portal → **Print** (`/me/print`): upload PDF, JPEG, PNG, GIF or text. Nothing to install. |
| Linux | GNOME's graphical dialog can not take a user name and key: use the `lpadmin` command shown after "Create a key", or `scripts/add-printer-linux.sh <server> <queue> <user>`. |
| Android | **Mopria Print Service** discovers the printers and asks for name and key. The built-in Android print service cannot authenticate. |
| Windows, macOS, iPhone | Add a network printer `ipps://<server>:8631/printers/<queue>`; enter user name and key when asked. Windows 11 25H2 is tested ([windows.md](windows.md)); macOS and iPhone are not. |

The device must trust the certificate. Details in [printing.md](printing.md).

## 5. USB printer

**New USB printers are added by themselves** (within about a minute): plug the printer in and it appears in the Printers page. If it has
IPP-over-USB it is served by `ipp-usb`; if not (many older lasers), the `cups` container uses it directly and needs a driver — the image
includes brlaser, gutenprint, splix and foo2zjs (add others in `docker/stack/Dockerfile`). `CUPS_QUEUES` is only needed to give a
fixed name to a queue; a queue named there is created once, and stays deleted if you delete it later (or if the gateway removes an unplugged USB printer). To bring one back: `scripts/queue-restore.sh NAME --reboot`. See [printers.md](printers.md#2-new-and-unplugged-usb-printers).

`ipp-usb` needs exclusive use of the printer. If it is installed on the host, stop and mask it (SANE too):

```sh
sudo systemctl disable --now ipp-usb && sudo systemctl mask ipp-usb
```

Then in `.env`:

```
CUPS_QUEUES=HP7305=ipp://ipp-usb:60000/ipp/print
```

and `docker compose up -d`. The queue is created automatically as soon as the printer answers (retried for 5 minutes).
Further printers: `docker compose exec cups lpadmin -p NAME -E -v ipp://ipp-usb:600NN/ipp/print -m everywhere`
(each printer's port is shown by `docker compose logs ipp-usb`). Plugging or unplugging a printer restarts `ipp-usb`
by itself within seconds.

## 6. Network printer

Give `CUPS_QUEUES` the printer's IPP address: `CUPS_QUEUES="OFFICE=ipp://192.168.1.60/ipp/print"` (several queues are
separated by spaces). Scanners that answer eSCL on the printer's IPP port (or port 80) are found automatically;
otherwise list them in `SPOOLSTEWARD_SCAN_URL`.

## 7. Try it without a printer or RADIUS

```sh
# in .env:  CUPS_VIRTUAL_QUEUE=1   SPOOLSTEWARD_RADIUS_HOST=radius   SPOOLSTEWARD_RADIUS_SECRET=testing123
docker compose --profile radius-test up -d --build
```

Creates the queue `virtual` (writes PDFs, no paper) and a test RADIUS with `mario/mario123`, `anna/anna123`,
`admin/admin123`. Example: `ipptool -t -f file.txt "ipps://mario:mario123@localhost:8631/printers/virtual" job.test`.
PDFs: `docker compose exec cups ls /var/spool/cups-pdf/ANONYMOUS`. You can also create **test printers** from the
Printers page. Never leave test accounts on a real installation.

## 8. Updating, backing up, moving

- **Update** — see "Updates" below.
- **Backups**: the portal's *Backup* menu ([backup.md](backup.md)); `scripts/backup.sh backup` for a full host-side copy
  (database, config, audit archive, CUPS queues, `.env`) to move the machine.
- **Logs**: `docker compose logs gateway`. **Health**: `GET /healthz`.

### Updates

The images are published for each release (`ghcr.io/deh-aleh/spoolsteward:<version>`, plus `spoolsteward-updater` for the optional updater; amd64 + arm64) and
`.env` holds the version that runs (`SPOOLSTEWARD_VERSION`). A version is only changed **on purpose**: nothing updates by itself.
Database migrations run at start.

Each release also has a **package** (`spoolsteward-<version>.tar.gz` with its `SHA256SUMS`) holding `docker-compose.yml`, the scripts,
`docker/env.example` and the documentation. It never holds `.env`, `config/` or `data/`: an update cannot touch them.

| How | When |
|---|---|
| `scripts/update.sh` | **Normal way.** Makes a full backup (`data/backups/pre-update/`), downloads the package of the new version and checks its sha256 sum, replaces `docker-compose.yml` and the scripts (the old ones stay in `.update-prev/`), writes the new version in `.env`, downloads the images, restarts and waits until the gateway is healthy. If it is not, it puts the previous files and version back; if even the previous version does not start (the new one already changed the database), it offers to restore the backup it has just made. `scripts/update.sh 1.2.0` picks a version; `--check` only tells if a newer one exists. A change of major version (1.x → 2.x) asks for confirmation: read the release notes first. |
| Button *Update* in the portal | Optional. Menu *Backup*, box *Update*. Needs the `updater` container (below) **and** a scheduled backup that is on and succeeded in the last 48 h: a fresh backup is made, then the update starts. If there is no working scheduled backup the button stays off and says why. Only updates inside the same major version. If the new version does not start it puts the old one back, but restoring the backup, if needed, is up to you, with `scripts/backup.sh restore` (see "Going back to an older version" below). |
| `git pull && docker compose up -d --build` | Only in a folder cloned with git (development) and for images you build yourself (`SPOOLSTEWARD_REGISTRY` empty in `.env`). `scripts/update.sh` refuses to run in a folder with `.git`, so it does not fight with git. |

The compose file and the images always change together, so a new image never runs with an old compose file. `.env` is not touched.

**Notice of a new version.** Every 24 hours (the first time 2 minutes after the gateway starts, so a restart checks again at once) the gateway asks GitHub for the newest release and shows a note to administrators.
Only that one request goes out and it sends nothing about your server. Switch it off with `SPOOLSTEWARD_UPDATE_CHECK=false`.

**The optional `updater` container** applies updates asked for in the portal. It is **off by default** because it needs the
Docker socket (`/var/run/docker.sock`), and whoever controls that socket controls the whole machine (root): if someone found a flaw in
the portal and used it, they could command Docker and take the server. It only reads a version number `x.y.z` written by the gateway;
images always come from the registry in your `.env`. An update started from the portal does not replace the `updater` container itself (it is running the update): it keeps the version it was created with until you run `docker compose up -d` in a terminal. Turn it on with:

```sh
echo "COMPOSE_PROFILES=autoupdate" >> .env      # one line in .env (or add --profile autoupdate every time)
docker compose up -d
```

If you do not want that risk, keep it off and use `scripts/update.sh`. To turn it off again: remove the line from `.env` and run `docker compose stop updater`.

**New options in later versions.** An update never modifies your `.env`. A new option works at once with its default value even if it is not
in your `.env`. To see which options exist, open `docker/env.example` (every update replaces it): each option has its explanation. To change one,
copy its line into your `.env` and run `docker compose up -d`.

**Going back to an older version.** `scripts/update.sh 0.3.0` puts the files and images of that version back. From 0.3.1 on, a version put back this way starts on a database the newer one already migrated (migrations only add). Versions up to 0.3.0 do not: they refuse to start on a database they do not know. In that case, and in any case where the new version damaged something, use the backup that `update.sh` made **before** the update (emergency procedure):

```sh
ls data/backups/pre-update/
#   spoolsteward-v0.3.0-20261016-132831.tar.gz   <- made while 0.3.0 was running
#   spoolsteward-v0.2.2-20261016-101500.tar.gz   <- made while 0.2.2 was running (before the update to 0.3.0)
scripts/backup.sh restore data/backups/pre-update/spoolsteward-v0.2.2-20261016-101500.tar.gz    # asks you to type YES
scripts/update.sh 0.2.2                    # puts the files of that version back (the restore brought back its .env)
```

The version in the file name is the one that was running when the backup was made: to go back to a version, take the backup with that name. Everything done after that backup (print history, new users, requests) is lost; the backup also brings back the `.env` of that moment. Each attempt makes a new backup first, so nothing is overwritten: the newest file is always the state just before your last command.

**Betas.** A version such as `0.4.0-beta.1` is a test release, published as a GitHub pre-release. `scripts/update.sh` without arguments, the *Update* button and the "new version" note never offer it; you get it only by naming it: `scripts/update.sh 0.4.0-beta.1`. From a beta, the final release (`0.4.0`) is offered normally. Do not use a beta on a server people depend on.

**Pin the version.** Never use `latest`: leave `SPOOLSTEWARD_VERSION` at a number, so an update is a decision.

## 9. Alternative: install on the host (no docker)

> This **modifies the system**: it creates the user `spoolsteward`, files in `/opt` and `/etc`, a systemd unit, and replaces
> `/etc/cups/cupsd.conf` (the original is saved as `cupsd.conf.pre-gateway`).

```sh
sudo sh deploy/install.sh                 # user, venv in /opt/spoolsteward, /etc/spoolsteward/env, unit file
sudoedit /etc/spoolsteward/env     # RADIUS (optional), TLS paths, …; put the certificate in /etc/spoolsteward/tls
sudo sh deploy/setup-cups.sh              # makes CUPS a private backend (unix socket, no TCP); --revert goes back
sudo systemctl enable --now spoolsteward
journalctl -u spoolsteward | grep "FIRST RUN"
```

Needs Python ≥ 3.12 with `venv`. The service runs as `spoolsteward`, sandboxed, and binds port 631 through
`CAP_NET_BIND_SERVICE`. `deploy/env.example` lists the settings. The mDNS announcer is a separate process
(`python -m app.mdns`) that you must run yourself if you want it.

## 10. Development

```sh
pip install -e '.[dev]'
docker compose -f docker-compose.test.yml up -d --build   # test CUPS on :6310 and FreeRADIUS on :1812
pytest                                                    # tests needing docker skip themselves if it is off
```
