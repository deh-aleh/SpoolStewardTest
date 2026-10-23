# TLS certificates: notices and synchronisation

[Documentation index](../README.md) · English · [Italiano](../it/certificati.md)

The gateway serves IPPS and the portal over TLS only. This page covers the certificate requirements, the notices raised when it is
wrong, and `scripts/cert-sync.sh`, which copies a certificate renewed on another machine (for example Nginx Proxy Manager).

## 1. What clients validate

A client checks three things before it sends anything; if one fails it refuses the connection or warns:

1. **Chain**: issued by a CA the device trusts (public CA, or the company CA installed on the device, §3).
2. **Name**: the host the client connects to is in the certificate's Subject Alternative Name (SAN), as a DNS name or an IP address.
3. **Validity**: `notAfter` is in the future.

The announcer (mDNS) publishes the IP of the default route, or `SPOOLSTEWARD_MDNS_ADDRESSES`, and the host name of `SPOOLSTEWARD_MDNS_HOSTNAME`
(default: the host of `SPOOLSTEWARD_PORTAL_URL`, otherwise the machine name). Clients that discover the printer through mDNS connect to that
address, so it must be in the SAN. Give the server a fixed address: if DHCP changes it, the certificate no longer matches.

**Browsers and HTTPS only (HSTS).** With `SPOOLSTEWARD_HSTS_SECONDS` above `0` (**off by default**; try `2592000`, 30 days, then `docker compose up -d`)
the portal tells the browser to use only HTTPS with this **host name**:

- **A certificate the browser does not trust** (the self-signed one of `docker-init.sh`): the browser **ignores** the header.
- **A certificate it trusts** (a real authority, or the company CA, §3): the browser remembers it. `http://print.lan` becomes `https://print.lan`
  by itself and **a certificate error can no longer be clicked through**. If the certificate expires and nobody renews it, the browser shows
  "your connection is not private" without the "continue" link, so nobody reaches the portal to see the notice. Once the certificate is renewed
  it works again at once.
- **It applies to the host name, whatever the port.** A plain-`http` page of the same name on another port stops opening.

To turn it off set `SPOOLSTEWARD_HSTS_SECONDS=0`: browsers keep the rule until its time is over (30 days at most), until they receive the header with
`0` over valid HTTPS, or until the site's data is cleared. With a self-renewing certificate you can later raise it to `31536000` (one year).

## 2. Notices in the portal (the bell)

The gateway never replaces the certificate. Every 6 hours it reads `SPOOLSTEWARD_TLS_CERTFILE` and `config/tls/sync-status.json` and opens or closes
notices for administrators; a notice closes by itself when its cause is gone.

| Notice | Condition | Switch |
|---|---|---|
| **The TLS certificate has expired** | `notAfter` is in the past | always on |
| **The certificate could not be updated** | the last `scripts/cert-sync.sh` run wrote `"ok": false` in `sync-status.json` (the message is in the notice) | only if the script is used |
| **The announced address is not in the TLS certificate** | none of the announced addresses/names is in the SAN; wildcards (`*.example.com`) cover one label | `SPOOLSTEWARD_CERT_NAME_CHECK=false` turns it off |
| **The TLS certificate expires on DATE** | fewer than `SPOOLSTEWARD_CERT_EXPIRY_WARN_DAYS` days left | **off by default**: `SPOOLSTEWARD_CERT_EXPIRY_WARN_ENABLED=true` |

The expiry notice adds a hint from the last synchronisation: if it succeeded, the source has not renewed; if it failed, see the other notice; if the script
is not used, renew and put the files in `config/tls`.

**Why the expiry notice is off by default.** Let's Encrypt certificates last 90 days and are renewed about 30 days before `notAfter`. With the threshold at
14 days the notice appears only when the renewal has been failing for over two weeks; above 30 it would fire at every normal cycle. A company CA
certificate often lasts a year and is renewed by hand: there 30 days is reasonable.

## 3. The CA file: what to do with it

For a company CA, install the **CA certificate** (a `.crt` or `.pem` file; never the private key) once on every device:

| Device | What to do |
|---|---|
| Android | *Settings → Security → Encryption & credentials → Install a certificate → CA certificate*. The browser accepts it; **some apps ignore user-added CAs**: then use a public CA (for example Let's Encrypt). |
| iPhone / iPad | Open the file, *Settings → Profile Downloaded → Install*. Then *Settings → General → About → Certificate Trust Settings* and switch the CA on: without this the certificate stays untrusted. |
| Windows | Double-click the file → *Install Certificate* → *Local Machine* → *Trusted Root Certification Authorities*. |
| macOS | *Keychain Access*: drag the file into *System*, double-click it → *Trust* → *Always Trust*. |
| Linux (Debian, Ubuntu) | Copy it to `/usr/local/share/ca-certificates/` as `.crt`, then `sudo update-ca-certificates`. Firefox has its own store: *Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import*. |

With the self-signed certificate of `scripts/docker-init.sh` the file to install is `config/tls/fullchain.pem`. A public CA (Let's Encrypt) needs nothing on the devices.

## 4. Copying the certificate by hand

Put `fullchain.pem` (certificate and chain) and `privkey.pem` in `config/tls/`, make the key readable only by the `PUID` user of `.env`
(`chmod 600 config/tls/privkey.pem`) and run `docker compose restart gateway`.

**You already have a valid certificate (for example from Nginx Proxy Manager on another machine).** Let the gateway use it directly:

1. Make a name that certificate covers (say `print.example.com`) resolve to the server inside your network.
2. Copy the pair (NPM: `data/letsencrypt/live/npm-<n>/` or, for custom certificates, `data/custom_ssl/npm-<n>/`) to `config/tls/`, `chmod 600` the key, and run `docker compose restart gateway`.
3. Set `SPOOLSTEWARD_PORTAL_URL=https://print.example.com:8631` in `.env` and use `ipps://print.example.com:8631/printers/<queue>`.

Devices that find printers through mDNS get the `<name>.local` name or the IP, which that certificate does not cover: use the real name by hand there, or stop
the `mdns` service if you do not need discovery. Let's Encrypt renews about every 60 days: for that case use §5.

### 4.1 Behind a reverse proxy

A reverse proxy can keep the certificate and forward to `https://<server>:8631` (not tested by the project). It must forward to scheme `https`, allow
large bodies and not buffer them (`client_max_body_size 250m; proxy_request_buffering off; proxy_read_timeout 600s;`). The gateway sees every client as the
proxy's address (login lock-out, audit IPs) unless `FORWARDED_ALLOW_IPS=<proxy address>` is set in `.env`. Printer addresses given back to clients carry the
`Host` the proxy forwards, without `:443`, so devices must keep the address they were set up with.

## 5. Keeping the certificate up to date by itself: `scripts/cert-sync.sh`

### 5.1 How it works

Run it once by hand, then from `cron`. Each run:

1. downloads the two files from the machine that renews the certificates (`rsync` over `ssh`) into a private temporary folder;
2. checks them: the certificate is not expired and the key is really its own;
3. if they equal the ones in use, **stops** (no restart);
4. otherwise puts the old pair in `config/tls/previous/`, installs the new one and restarts the gateway;
5. writes the outcome to `config/tls/sync-status.json`; if it went badly the portal shows a notice.

If a check fails, **the files in use are not touched**.

### 5.2 The settings (in `.env`)

`CERT_SYNC_HOST`, `CERT_SYNC_USER`, `CERT_SYNC_KEY` (full path of the ssh private key of this machine) and `CERT_SYNC_REMOTE_DIR` (full path of the folder with
the two files) are required; `CERT_SYNC_PORT` (`22`), `CERT_SYNC_CERT_FILE` (`fullchain.pem`) and `CERT_SYNC_KEY_FILE` (`privkey.pem`) are optional
([configuration.md](configuration.md#certificate-synchronisation-cert_sync_)). The folder depends on the program that renews:

| Program | `CERT_SYNC_REMOTE_DIR` (example) | Files |
|---|---|---|
| Nginx Proxy Manager, Let's Encrypt | `/home/you/npm/data/letsencrypt/live/npm-3` | `fullchain.pem`, `privkey.pem` |
| Nginx Proxy Manager, custom certificate | `/home/you/npm/data/custom_ssl/npm-3` | `fullchain.pem`, `privkey.pem` |
| certbot | `/etc/letsencrypt/live/yourdomain.com` | `fullchain.pem`, `privkey.pem` |
| Other (Caddy, Traefik, acme.sh, company CA) | where the program saves them | the names it uses |

The number in `npm-3` differs on every installation: look inside `live/` on the NPM machine. To find NPM's data folder:
`docker inspect CONTAINER-NAME --format '{{ range .Mounts }}{{ .Source }} -> {{ .Destination }}{{ println }}{{ end }}'` (the one ending in `/etc/letsencrypt`).

### 5.3 Step by step: a user that can only read

The private key is a secret, so the script does not use an administrator's account: a stolen ssh key would give full access to the source machine.
Create a separate user that can only read the certificates. In the example the NPM machine is `npm.lan`, the SpoolSteward one `print.lan` (`192.168.1.50`), the user `certsync`.

**1 — on `npm.lan`: create the user** (no password).

```sh
sudo adduser --disabled-password --gecos "" certsync
sudo apt install rsync acl          # if missing
```

**2 — on `print.lan`: create the ssh key** (as the user that will run cron; `rsync` and `ssh` are needed here too).

```sh
ssh-keygen -t ed25519 -N "" -C "spoolsteward cert-sync" -f ~/.ssh/spoolsteward_certsync
cat ~/.ssh/spoolsteward_certsync.pub          # one line starting with ssh-ed25519
```

The private key must not leave this machine; its full path goes into `CERT_SYNC_KEY`.

**3 — on `npm.lan`: authorise the key, with limits.** Use the IP of `print.lan` and the line printed in step 2.

```sh
sudo -u certsync mkdir -p -m 700 /home/certsync/.ssh
echo 'restrict,from="192.168.1.50" ssh-ed25519 AAAA… spoolsteward cert-sync' | sudo -u certsync tee -a /home/certsync/.ssh/authorized_keys
sudo chmod 600 /home/certsync/.ssh/authorized_keys
```

`from=` makes the key work **only** from `print.lan`; `restrict` removes everything else (port forwarding, terminal, ssh agent).

**4 — on `npm.lan`: read access on the certificates, nothing else** (POSIX ACLs; example for NPM with data in `/home/you/npm/data`):

```sh
sudo setfacl -m u:certsync:x /home/you /home/you/npm /home/you/npm/data          # walk through the folders above
sudo setfacl -R -m u:certsync:rX /home/you/npm/data/letsencrypt
sudo setfacl -R -d -m u:certsync:rX /home/you/npm/data/letsencrypt              # also for files created at the next renewals
```

For certbot: `sudo setfacl -R -m u:certsync:rX /etc/letsencrypt` (and the same with `-d`). If the disk does not support ACLs, use a group that can read the folder and add `certsync` to it.
Check:

```sh
sudo -u certsync head -1 /home/you/npm/data/letsencrypt/live/npm-3/fullchain.pem     # -----BEGIN CERTIFICATE-----
sudo -u certsync touch /home/you/npm/data/letsencrypt/live/npm-3/test                 # Permission denied
```

**5 — on `print.lan`: settings in `.env`.**

```sh
CERT_SYNC_HOST=npm.lan
CERT_SYNC_USER=certsync
CERT_SYNC_KEY=/home/you/.ssh/spoolsteward_certsync
CERT_SYNC_REMOTE_DIR=/home/you/npm/data/letsencrypt/live/npm-3
```

**6 — on `print.lan`: try it,** first without changing anything: `scripts/cert-sync.sh --dry-run`. The first time ssh remembers the fingerprint of `npm.lan` (in
`config/ssh/known_hosts`); if it ever changes the script refuses to connect. It says "a new certificate is available" or "already up to date". Then the real run:
`scripts/cert-sync.sh`.

**7 — on `print.lan`: cron.** `crontab -e` as your own user (it must be able to use `docker`), then add (change the path):

```
17 4 * * *  cd /home/you/spoolsteward && scripts/cert-sync.sh >> data/cert-sync.log 2>&1
```

Once a day is enough: with an unchanged certificate nothing is installed and nothing restarts.

### 5.4 If something goes wrong

The message appears in the bell and in `data/cert-sync.log`; to see it now run `scripts/cert-sync.sh --dry-run`.

| Message | What it means and what to do |
|---|---|
| `set CERT_SYNC_HOST, CERT_SYNC_USER, CERT_SYNC_KEY and CERT_SYNC_REMOTE_DIR in .env` | A required setting is missing. |
| `the ssh key … does not exist or can not be read by this user` | `CERT_SYNC_KEY` is wrong (it must be the full path) or the file belongs to another user. |
| `could not download … Permission denied (publickey)` | The public key is not in the remote `authorized_keys`, or `from=` does not match this machine's IP. Redo step 3. |
| `could not download … REMOTE HOST IDENTIFICATION HAS CHANGED` | The remote fingerprint changed (reinstalled, or someone is pretending to be it). If you know why, delete its line from `config/ssh/known_hosts`. |
| `could not download … No such file or directory` | `CERT_SYNC_REMOTE_DIR` or the file names are wrong (the `npm-3` number, for example). |
| `could not download … Permission denied` (without `publickey`) | The remote user can not read: redo step 4. |
| `the certificate on … is already expired: it was not installed` | The source still has an expired certificate: find out why it does not renew. |
| `the private key … does not belong to the certificate …: nothing was installed` | The key and certificate files are not the same pair. The files in use were not touched. |
| `the new certificate is installed but the gateway did not restart` | Run `docker compose restart gateway` by hand and read `docker compose logs gateway`. If cron can not use docker, add its user to the `docker` group. |

To go back to the previous pair: copy the two files of `config/tls/previous/` to `config/tls/` and run `docker compose restart gateway`.

### 5.5 What the script does not do

- It does not **renew** the certificate: it copies it from whoever renews it. To notice a failed renewal, turn on the "about to expire" notice (§2).
- It does not change the names in the certificate: if the announced address is missing, the certificate must be reissued at the source.
- It does not touch the devices' CA (§3).
- It does not use `rrsync`: `rrsync` refuses symbolic links, and both Nginx Proxy Manager and certbot keep links in `live/`. The limits are those of steps 3 and 4.
