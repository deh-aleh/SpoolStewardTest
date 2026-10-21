# TLS certificates: notices and synchronisation

[Documentation index](../README.md) · English · [Italiano](../it/certificati.md)

The gateway serves IPPS and the portal over TLS only. This page covers the certificate requirements, the notices raised when it is
wrong, and `scripts/cert-sync.sh`, which copies a certificate renewed on another machine (for example Nginx Proxy Manager).

## 1. What clients validate

A client validates three things before it sends anything; if one fails it refuses the connection or shows a warning:

1. **Chain**: the certificate is issued by a CA the device trusts (public CA, or the company CA installed on the device, §3).
2. **Name**: the host the client connects to appears in the Subject Alternative Name (SAN) of the certificate, as a DNS name or an IP address.
3. **Validity**: `notAfter` is in the future.

The announcer (mDNS) publishes the IP address of the default route, or `SPOOLSTEWARD_MDNS_ADDRESSES`, and the host name from `SPOOLSTEWARD_MDNS_HOSTNAME`
(default: the host part of `SPOOLSTEWARD_PORTAL_URL`, otherwise the machine name). Clients that discover the printer through mDNS connect to that
address, so it must be in the SAN. If the address changes (DHCP), the certificate no longer matches: give the server a fixed address.

## 2. Notices in the portal (the bell)

The gateway never replaces the certificate. It reads `SPOOLSTEWARD_TLS_CERTFILE` and `config/tls/sync-status.json` every 6 hours and
opens or closes notices for administrators. A notice closes by itself when its cause is gone.

| Notice (title in the bell) | Condition | Switch |
|---|---|---|
| **The TLS certificate has expired** | `notAfter` is in the past | always on |
| **The certificate could not be updated** | the last run of `scripts/cert-sync.sh` wrote `"ok": false` in `sync-status.json` (its message is in the notice) | only appears if the script is used |
| **The announced address is not in the TLS certificate** | none of the announced addresses/names (mDNS) is in the SAN; wildcards (`*.example.com`) cover one label | `SPOOLSTEWARD_CERT_NAME_CHECK=false` turns it off |
| **The TLS certificate expires on DATE** | fewer than `SPOOLSTEWARD_CERT_EXPIRY_WARN_DAYS` days left | **off by default**: `SPOOLSTEWARD_CERT_EXPIRY_WARN_ENABLED=true` |

The expiry notice adds a hint based on the last synchronisation: if it succeeded, the source has not renewed (check the renewing program);
if it failed, it points to the other notice; if the script is not used, it asks to renew and put the files in `config/tls`.

**Why the expiry notice is off by default.** A Let's Encrypt certificate lasts 90 days and is renewed about 30 days before `notAfter`. With the
threshold at 14 days the notice only appears when the renewal has been failing for more than two weeks. Above 30 days it would fire at every
normal renewal cycle. Certificates of a company CA often last a year and are renewed by hand: there 30 days is reasonable.

The settings are in `.env`; apply them with `docker compose up -d`.

## 3. The CA file: what to do with it

For a company CA, install the **CA certificate** (a `.crt` or `.pem` file; never the private key) once on every device. Devices then trust
every certificate that CA issues, the gateway's included:

| Device | What to do |
|---|---|
| Android | *Settings → Security → Encryption & credentials → Install a certificate → CA certificate*, pick the file. The browser accepts it; **some apps ignore CAs added by the user**: if an app refuses to connect, use a certificate from a public CA (for example Let's Encrypt). |
| iPhone / iPad | Open the file (from Mail or Files), *Settings → Profile Downloaded → Install*. Then *Settings → General → About → Certificate Trust Settings* and switch the CA on: without this second step the certificate stays untrusted. |
| Windows | Double-click the file → *Install Certificate* → *Local Machine* → *Place all certificates in the following store* → *Trusted Root Certification Authorities*. |
| macOS | Open *Keychain Access*, drag the file into *System*, double-click the certificate → *Trust* → *Always Trust*. |
| Linux (Debian, Ubuntu) | Copy the file to `/usr/local/share/ca-certificates/` with the extension `.crt`, then `sudo update-ca-certificates`. Firefox has its own store: *Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import*. |

If you have no CA and use the self-signed certificate made by `scripts/docker-init.sh`, the file to install is `config/tls/fullchain.pem`, with the same steps.

With a certificate from a public CA (Let's Encrypt) there is nothing to install on the devices: they already trust it.

## 4. Copying the certificate by hand

Put `fullchain.pem` (the certificate, with its chain) and `privkey.pem` (the key) in `config/tls/`, make the key readable only by you
(`chmod 600 config/tls/privkey.pem`) and restart: `docker compose restart gateway`. The key must be readable by the `PUID` user of `.env`.

**You already have a valid certificate (for example one issued by Nginx Proxy Manager on another machine).** The simplest way is to let the
gateway use that same certificate itself, with no proxy in between:

1. Make a name that certificate covers (say `print.example.com`) resolve to the server inside your network (local DNS record or router entry).
2. Copy the pair from the machine that holds it (NPM: `data/letsencrypt/live/npm-<n>/fullchain.pem` and `privkey.pem`; custom certificates:
   `data/custom_ssl/npm-<n>/`) to `config/tls/fullchain.pem` and `config/tls/privkey.pem`, make the key readable by the `PUID` user
   (`chmod 600`), and run `docker compose restart gateway`.
3. Set `SPOOLSTEWARD_PORTAL_URL=https://print.example.com:8631` in `.env`, and use `ipps://print.example.com:8631/printers/<queue>`.

Devices that find printers through mDNS are given the `<name>.local` name or the IP, which a certificate for `print.example.com` does not
cover: use the real name by hand on those devices, or stop the `mdns` service if you do not need discovery.

A Let's Encrypt certificate is renewed about every 60 days; for that case use the script of §5.

### 4.1 Behind a reverse proxy

Instead of giving the certificate to the gateway, a reverse proxy can keep it and forward to `https://<server>:8631` (not tested by the
project). The proxy must forward to scheme `https`, allow large bodies and not buffer them (`client_max_body_size 250m;
proxy_request_buffering off; proxy_read_timeout 600s;`). The gateway sees every client as the proxy's address (login lock-out per address,
audit IPs) unless `FORWARDED_ALLOW_IPS=<proxy address>` is set in `.env`, so that it trusts `X-Forwarded-For`. Printer addresses given back
to clients carry the `Host` the proxy forwards, without `:443`, so devices must keep the address they were set up with.

## 5. Keeping the certificate up to date by itself: `scripts/cert-sync.sh`

### 5.1 How it works

Run it once by hand, then from `cron`. Each run:

1. downloads the two files from the machine that renews the certificates (with `rsync` over `ssh`) into a private temporary folder;
2. checks them: the certificate is not expired and the private key is really its own;
3. if they are the same as the ones in use, it **stops** (nothing to do, no restart);
4. otherwise it puts the old pair aside in `config/tls/previous/`, installs the new one and restarts the gateway;
5. writes how it went to `config/tls/sync-status.json`: if it went badly, the portal shows the notice "the last certificate update failed".

If something is wrong in the checks, **the files in use are not touched**: a wrong certificate cannot break a server that works.

### 5.2 The settings (in `.env`)

| Setting | What | Default |
|---|---|---|
| `CERT_SYNC_HOST` | The machine that has the certificate (name or address) | required |
| `CERT_SYNC_USER` | The user to connect as (the read-only one of §5.3) | required |
| `CERT_SYNC_KEY` | **Full** path of the ssh private key of this machine | required |
| `CERT_SYNC_REMOTE_DIR` | **Full** path, on the remote machine, of the folder that holds the two files | required |
| `CERT_SYNC_PORT` | ssh port | `22` |
| `CERT_SYNC_CERT_FILE` | Name of the certificate file (with the chain) | `fullchain.pem` |
| `CERT_SYNC_KEY_FILE` | Name of the key file | `privkey.pem` |

The path and the names change with the program that renews the certificates, which is why they can be chosen:

| Program | `CERT_SYNC_REMOTE_DIR` (example) | Files |
|---|---|---|
| Nginx Proxy Manager, Let's Encrypt certificate | `/home/you/npm/data/letsencrypt/live/npm-3` | `fullchain.pem`, `privkey.pem` |
| Nginx Proxy Manager, custom certificate | `/home/you/npm/data/custom_ssl/npm-3` | `fullchain.pem`, `privkey.pem` |
| certbot | `/etc/letsencrypt/live/yourdomain.com` | `fullchain.pem`, `privkey.pem` |
| Other (Caddy, Traefik, acme.sh, a company CA) | the folder where the program saves the files | the names it uses, for example `cert.pem` and `key.pem` |

The number in `npm-3` is different on every installation: look at which folder is inside `live/` on the NPM machine. To find where NPM keeps its data:
`docker inspect CONTAINER-NAME --format '{{ range .Mounts }}{{ .Source }} -> {{ .Destination }}{{ println }}{{ end }}'` (look for the one ending in `/etc/letsencrypt`).

### 5.3 Step by step: a user that can only read

The private key is a secret, so the script does not use an administrator's account: a stolen ssh key would give full access to the source machine.
Create a separate user that can only read the certificates.

In the example the NPM machine is called `npm.lan`, the SpoolSteward one `print.lan` (IP `192.168.1.50`), the user is called `certsync`.

**Step 1 — on `npm.lan`: create the user** (no password: it is entered only with the key).

```sh
sudo adduser --disabled-password --gecos "" certsync
sudo apt install rsync acl          # if missing: rsync does the copying, acl is for the permissions of step 4
```

**Step 2 — on `print.lan`: create the ssh key** (as your own user, the one that will run cron).
`rsync` and `ssh` are needed on this machine too (`sudo apt install rsync openssh-client` if missing).

```sh
ssh-keygen -t ed25519 -N "" -C "spoolsteward cert-sync" -f ~/.ssh/spoolsteward_certsync
cat ~/.ssh/spoolsteward_certsync.pub          # one line starting with ssh-ed25519: we use it in step 3
```

The private key (`spoolsteward_certsync`, without `.pub`) must not leave this machine. Its full path (for example `/home/you/.ssh/spoolsteward_certsync`)
goes into `CERT_SYNC_KEY`.

**Step 3 — on `npm.lan`: authorise the key, with limits.** Replace `192.168.1.50` with the IP of `print.lan` and the `ssh-ed25519 AAAA…` part with the line printed in step 2.

```sh
sudo -u certsync mkdir -p -m 700 /home/certsync/.ssh
echo 'restrict,from="192.168.1.50" ssh-ed25519 AAAA… spoolsteward cert-sync' | sudo -u certsync tee -a /home/certsync/.ssh/authorized_keys
sudo chmod 600 /home/certsync/.ssh/authorized_keys
```

The two words in front of the key are the limits: `from="192.168.1.50"` makes the key work **only** from `print.lan`; `restrict` removes everything else
(no port forwarding, no interactive terminal, no ssh agent).

**Step 4 — on `npm.lan`: give the user permission to read the certificates, and nothing else.** With POSIX ACLs, grant read access on one folder only.
Example for NPM, with the data in `/home/you/npm/data`:

```sh
# the user must be able to "walk through" the folders above (x) and read (r) the ones with the certificates
sudo setfacl -m u:certsync:x /home/you /home/you/npm /home/you/npm/data
sudo setfacl -R -m u:certsync:rX /home/you/npm/data/letsencrypt
sudo setfacl -R -d -m u:certsync:rX /home/you/npm/data/letsencrypt    # also valid for the files created at the next renewals
```

For certbot: `sudo setfacl -R -m u:certsync:rX /etc/letsencrypt` (and the same line with `-d`).

If your disk does not support ACLs (`setfacl` answers "Operation not supported"), the alternative is to create a group that can read the folder and add `certsync` to that group.

Check that it works and that it is really read-only:

```sh
sudo -u certsync head -1 /home/you/npm/data/letsencrypt/live/npm-3/fullchain.pem     # must print -----BEGIN CERTIFICATE-----
sudo -u certsync touch /home/you/npm/data/letsencrypt/live/npm-3/test                 # must answer: Permission denied
```

**Step 5 — on `print.lan`: write the settings in `.env`.**

```sh
CERT_SYNC_HOST=npm.lan
CERT_SYNC_USER=certsync
CERT_SYNC_KEY=/home/you/.ssh/spoolsteward_certsync
CERT_SYNC_REMOTE_DIR=/home/you/npm/data/letsencrypt/live/npm-3
```

**Step 6 — on `print.lan`: try it.** First without changing anything:

```sh
scripts/cert-sync.sh --dry-run
```

The first time, ssh remembers the fingerprint of `npm.lan` (in the file `config/ssh/known_hosts`) and checks it on every later run: if one day that fingerprint changes, the script
refuses to connect (it is the sign that someone is pretending to be `npm.lan`). If all is well it says "a new certificate is available" or "already up to date".
Then the real test: `scripts/cert-sync.sh`. Open the portal: no notice about the certificate, and if the certificate had changed the gateway has restarted.

**Step 7 — on `print.lan`: put it in cron.** As your own user (it must be able to use `docker`):

```sh
crontab -e
```

and add a line (change the path of the SpoolSteward folder):

```
17 4 * * *  cd /home/you/spoolsteward && scripts/cert-sync.sh >> data/cert-sync.log 2>&1
```

This runs daily at 4:17 and appends the output to `data/cert-sync.log`. Once a day is enough: if the certificate is unchanged nothing is installed and nothing restarts.

### 5.4 If something goes wrong

The message appears in the portal (bell) and in `data/cert-sync.log`. To see it right away: `scripts/cert-sync.sh --dry-run`.

| Message | What it means and what to do |
|---|---|
| `set CERT_SYNC_HOST, CERT_SYNC_USER, CERT_SYNC_KEY and CERT_SYNC_REMOTE_DIR in .env` | One of the four required settings is missing. |
| `the ssh key … does not exist or can not be read by this user` | The path in `CERT_SYNC_KEY` is wrong or the file belongs to another user. It must be the full path. |
| `could not download … Permission denied (publickey)` | The public key is not in the remote user's `authorized_keys`, or `from=` does not match this machine's IP. Redo step 3. |
| `could not download … REMOTE HOST IDENTIFICATION HAS CHANGED` | The fingerprint of the remote machine changed (reinstalled, or someone is pretending to be it). If you know why, delete its line from `config/ssh/known_hosts`. |
| `could not download … No such file or directory` | `CERT_SYNC_REMOTE_DIR` or the file names are wrong (the `npm-3` number, for example). |
| `could not download … Permission denied` (without `publickey`) | The remote user is not allowed to read: redo step 4. |
| `the certificate on … is already expired: it was not installed` | The source still has an expired certificate: the problem is there, find out why it does not renew. |
| `the private key … does not belong to the certificate …: nothing was installed` | The key file and the certificate file are not the same pair (wrong names, or a renewal half done). The files in use were not touched. |
| `the new certificate is installed but the gateway did not restart` | The new certificate is in place, but `docker compose restart gateway` failed: run it by hand and look at `docker compose logs gateway`. If cron cannot use docker, add its user to the `docker` group. |

To go back to the previous pair: copy the two files of `config/tls/previous/` to `config/tls/` and run `docker compose restart gateway`.

### 5.5 What the script does not do

- It does not **renew** the certificate: it copies it from whoever renews it. If the renewal fails at the source, the old one keeps arriving here; to notice, turn on the "about to expire" notice (§2).
- It does not change the names in the certificate: if the address the gateway announces is missing, the notice says so, but the certificate must be reissued at the source.
- It does not touch the devices' CA (§3).
- It does not use `rrsync` to restrict the key further: `rrsync` refuses to follow symbolic links, and both Nginx Proxy Manager and certbot keep links in `live/` that point into `archive/`.
  The limits here are those of step 3 (`restrict`, `from=`) and of step 4 (read-only, on the certificates only).
