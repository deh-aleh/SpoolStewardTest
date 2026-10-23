# Troubleshooting

[Documentation index](../README.md) · English · [Italiano](../it/risoluzione-problemi.md)

First, collect facts:

```sh
docker compose ps                                  # are all services up? is cups "healthy"?
docker compose logs --tail=100 gateway             # the application log
docker compose logs --tail=100 cups ipp-usb mdns
curl -sk https://<server>:8631/healthz             # 200 = gateway and database ok, 503 = not
```

Increase detail with `SPOOLSTEWARD_LOG_LEVEL=DEBUG` in `.env`, then `docker compose up -d`.

CPU and RAM: `scripts/resources.sh` lists the containers and Docker itself and adds them up. If it shows 0 MB everywhere, the kernel does not measure memory per container: on a Raspberry Pi add `cgroup_enable=memory cgroup_memory=1` to the one line of `/boot/firmware/cmdline.txt` and reboot.

Each service keeps only its newest 30 MB of log (3 files of 10 MB): older lines are gone, so look soon after a problem.

## 1. The gateway does not start

| Symptom | Cause and fix |
|---|---|
| `refusing to start without TLS` | There is no certificate. Run `scripts/docker-init.sh` or put `fullchain.pem` + `privkey.pem` in `config/tls/`. |
| `refusing to start: wrong settings …` followed by one line per setting, for example `SPOOLSTEWARD_ESCL_WINDOW_SECONDS: Input should be greater than or equal to 60 (found: '30')` | A line of `.env` has a value the program does not accept (see [configuration.md](configuration.md)). Every wrong line is listed at once, but `docker compose up -d` does not show it: `docker compose ps` shows the portal restarting, and `docker compose logs --tail=20 gateway` prints the list. Fix the lines and run `docker compose up -d` again. |
| `SPOOLSTEWARD_RADIUS_HOST and SPOOLSTEWARD_RADIUS_SECRET: set both, or write none in both` | RADIUS is half configured. Fill both, or write `none` in both. |
| Permission denied on `config/tls/privkey.pem` | The key must be readable by the user `PUID:PGID` of `.env` (`chown`/`chmod 600`). |
| `Bind for 0.0.0.0:631 failed` | Another CUPS uses 631. Keep `PORTAL_PORT=8631`, or stop the other service. |
| "archive folder not usable" | `data/audit-archive` must be writable by the container user (same uid as `PUID`). |
| Everybody is logged out at each restart, or the bell says **No secret key is set** | `SPOOLSTEWARD_SECRET_KEY` is missing from `.env` or is `none`: see [No secret key is set](#no-secret-key-is-set) below. |
| `SPOOLSTEWARD_SECRET_KEY: still holds the placeholder of docker/env.example` | `docker/env.example` was copied to `.env` by hand, so the key is still the public `__GENERATED__`. Write the output of `openssl rand -hex 32` in its place, check `PUID`, `PGID` and `PROJECT_DIR` (the script would have filled them in), then `docker compose up -d`. |
| `SPOOLSTEWARD_SECRET_KEY: is shorter than 64 characters` | A key written by hand is too short. Write the output of `openssl rand -hex 32` in its place, then `docker compose up -d`. Everybody signs in again and file names encrypted with the old key show as `[encrypted]`. |

### No secret key is set

The administrators see a notice in the bell: `SPOOLSTEWARD_SECRET_KEY` is missing from `.env` or set to `none`.

**Cause.** `.env` comes from an older setup or was written by hand, and has no such line or has `none`. `scripts/docker-init.sh` writes a key only
when it creates `.env`; it never touches an existing one (it prints a warning that points here).

**What it means.** The gateway makes a new random key at every start: after each restart everybody signs in again, and `mdns` announces nothing
(without the key the announcer cannot prove who it is to the gateway). Nothing is lost: users, quotas and audit stay as they are.

**Fix.**

1. Open `.env` in the SpoolSteward folder.
2. If the line is `SPOOLSTEWARD_SECRET_KEY=none`, replace `none` with the output of `openssl rand -hex 32`. If there is no line, add it with the command
   below (only when it is missing: the file must hold one such line):

   ```sh
   printf 'SPOOLSTEWARD_SECRET_KEY=%s\n' "$(openssl rand -hex 32)" >> .env
   ```

3. Run `docker compose up -d`. Everybody signs in once more; the notice closes by itself.
4. Copy the key to a safe place, for example a password manager ([backup.md](backup.md#save-the-secret-key)).

## 2. Cannot sign in

| Symptom | Cause and fix |
|---|---|
| `docker compose logs gateway \| grep 'FIRST RUN'` prints nothing | Check in this order. **1.** `docker compose ps`: `gateway` must be *Up*; if it restarts, read `docker compose logs gateway \| tail -40`. **2.** The line is only printed while the administrator has no password. If this folder ran before, the database (volume `spoolsteward_portal-data`) still has the account: the log says *"administrator … already has a password"*; sign in at `https://<server>:8631/login`, or set a new password with `docker compose exec -it gateway spoolsteward-admin set-admin-password`. **3.** `SPOOLSTEWARD_BOOTSTRAP_ADMIN=false` switches the first-run account off. For a truly empty start (this **deletes all users, quotas and audit**): `docker compose down -v`, then `docker compose up -d`. |
| Locked after wrong passwords | 5 *different* wrong passwords in 5 minutes block the (user, IP) pair for 5 minutes, also across a restart. To clear it at once stop the gateway, delete `/data/login-lockout.json` in the volume and start it again. |
| A RADIUS user cannot log in | RADIUS unreachable ⇒ access denied. Check `SPOOLSTEWARD_RADIUS_*`, that this machine is a RADIUS *client* with the same secret, and the log. |
| Forced to change the password | Normal for temporary passwords created or reset by an administrator. |
| A user was disabled but still prints | Disabling an account in the portal stops it and all its keys at once. If the person was only removed from the RADIUS server, their print keys work until they expire: disable the account, or revoke the keys, in the portal. |

## 3. Printing

| Symptom | Cause and fix |
|---|---|
| The client asks for the password again and again | The **domain password does not work for printing**: use a print key from *Profile* (`SPOOLSTEWARD_IPP_AUTH=keys`). Or the pair (user, IP) is locked out. |
| `Forbidden` / HTML 426 from CUPS | CUPS policy is not the gateway's: use `docker/stack/cups/cupsd.conf` (`DefaultAuthType None`). |
| 502 from the gateway | CUPS is stopped or `SPOOLSTEWARD_CUPS_URL` is wrong. `docker compose logs cups`. |
| Job refused: "printing is allowed only…", "colour printing is not allowed…", "not allowed to use that printer" | The permissions of the user's group or own rule ([quotas.md](quotas.md#5-advanced-permissions)). The user's profile lists what is allowed and offers a request. |
| Job rejected with a quota message | The user reached a limit; the message includes the portal link to ask for more. See [quotas.md](quotas.md). |
| "printer is blocked" / "gateway is in maintenance" | An administrator blocked the printer or switched maintenance mode on ([printers.md](printers.md)). Administrators can still print during maintenance. |
| Job stays `accepted` forever | The reconciler cannot reach CUPS: look for `reconcile:` in the log. |
| The printer is shown NOT REACHABLE and is missing from the print form | The device does not answer (off, unplugged, USB asleep). Detection can lag ~30 s; for network queues it is a TCP check. |
| `:60000` does not answer | `ipp-usb` is not running or the printer is not seen: `docker compose logs ipp-usb`; replug the USB cable; make sure the **host's** `ipp-usb`/SANE are stopped and masked. |
| A new USB printer does not appear | Wait up to a minute or press **Look now** in Printers; check `docker compose logs gateway \| grep discovery`. If the page says "CUPS has no driver for it", add one to `docker/stack/Dockerfile`. Make sure the **host's** `ipp-usb`/SANE are stopped and `SPOOLSTEWARD_AUTO_DISCOVER` is not `false`. |
| An unplugged printer is still listed | It shows NOT REACHABLE with a countdown (USB queues, `SPOOLSTEWARD_OFFLINE_REMOVE_HOURS`) and a **Delete queue** button. Network queues are never removed by themselves. |
| A queue I deleted does not come back, although it is in `CUPS_QUEUES` | By design: each queue of `CUPS_QUEUES` (and `virtual`) is created once. To create it again: `scripts/queue-restore.sh NAME --reboot` (without `--reboot` the queue returns at the next start); `--list` shows the remembered names. The queue must still be in `CUPS_QUEUES`. |
| The queue was not created | `CUPS_QUEUES` is wrong, or the printer answered late. The entrypoint retries for 5 minutes; otherwise `docker compose exec cups lpadmin -p NAME -E -v URI -m everywhere`. |
| A "Create-Job" without a document keeps quota | Quota is freed only when CUPS cancels it (default after 3 h). |
| Page limit exceeded by one file | The limit is *soft* for files whose pages cannot be counted (unknown format, encrypted PDF, over 64 MB): see [limitations.md](limitations.md). |
| Browser print: "unsupported file" | Only PDF, JPEG, PNG, GIF and plain text are accepted, checked by content and not by the file name. |
| Browser print: "larger than N MB" | `SPOOLSTEWARD_WEB_UPLOAD_MB` (default 25). |

## 4. Phones and computers

| Symptom | Cause and fix |
|---|---|
| Certificate warning, or the device refuses to connect | The device does not trust the certificate. Install the CA, or use a CA-signed certificate in `config/tls/`. The name or IP used must be in the certificate. |
| The printers do not appear on the phone | `mdns` must run with host networking on the same LAN (`docker compose logs mdns`; Docker Desktop cannot). Set `SPOOLSTEWARD_MDNS_ADDRESSES` if the machine has several network cards, and `SPOOLSTEWARD_MDNS_HOSTNAME` to a name in the certificate. A blocked-hidden or unreachable printer, or maintenance mode, also removes the announcement. If the log says **"mDNS is OFF"**, `SPOOLSTEWARD_SECRET_KEY` is missing in `.env` ([above](#no-secret-key-is-set)). |
| Android: the system print service sees the printer but fails | It cannot authenticate. Install **Mopria Print Service**, or print from the portal. |
| Linux desktop shows the printers but nothing prints | Announced printers have no key. GNOME can not add the key from its dialog: run the `lpadmin` command from the "Create a key" page or `scripts/add-printer-linux.sh`, which store it in the queue address. |
| Windows: "Operation failed" or "connecting" for too long when adding the printer (full steps in [windows.md](windows.md)) | Windows asks whether the job would be accepted before it has a password, and the "Add device" window can not ask for one. Wait 3-4 minutes; if it does not connect: Control Panel → Credential Manager → Windows Credentials → Add a Windows credential; address = the printer's name or IP, user name = yours, password = the **print key** (not the account password). |
| macOS / iPhone | Not tested by the project. Try `ipps://<server>:8631/printers/<queue>`, user name + print key, and report what happens. |

## 5. Scanning

| Symptom | Cause and fix |
|---|---|
| No scanner in the portal | Scanners are found when they answer eSCL on the queue's IPP port or on port 80. Otherwise add `SPOOLSTEWARD_SCAN_URL=name=http://host:port`. A disconnected scanner disappears in ~30 s. |
| "scanner busy" | Another scan is in progress, or the previous eSCL job was not deleted by its client. The gateway retries 3 times when the device is waking. |
| Blank page from the feeder | The feeder was asleep and declared empty, so *Automatic* used the glass. Pick *Feeder* explicitly, or re-insert the sheets. |
| The scan window button is pressed but the scan is refused | The bell says "the window was for X, the request comes from Y": IPv4 against IPv6, NAT, or a proxy ([scanning.md](scanning.md)). Open the portal from the device that runs the scan app, with the same kind of address; behind a reverse proxy open it directly on port 8631. |
| There is no scan window button | `SPOOLSTEWARD_ESCL_WINDOW=false`, or no scanner is connected. |
| NAPS2 gets 401 without a prompt | It cannot send credentials. Set `SPOOLSTEWARD_ESCL_ANONYMOUS=true` (trusted networks only) and enter IP and port by hand. |
| The scan result is gone | Results stay in memory for `SPOOLSTEWARD_SCAN_KEEP_MINUTES`, only for their owner, and the oldest go first past `SPOOLSTEWARD_SCAN_KEEP_MB`. With `SPOOLSTEWARD_SCAN_DELETE_AFTER_DOWNLOAD=true` a scan can be downloaded once. Scan again and download sooner. |

## 6. Backups and audit

| Symptom | Cause and fix |
|---|---|
| Backup shows red | It failed or the last one is older than 48 h. Open the page for the reason; usually the folder is not writable or the disk is full. |
| "Folder name not valid…", "Folder is outside the backup root", "Folder … is not writable by the gateway" | A backup folder must be a sub-folder of `/backups` (host `data/backups`) and writable by the `PUID` user. A USB disk or a network share must be mounted on the host *inside* that folder **before** the gateway starts (restart the gateway after mounting). |
| Lost the passphrase | Encrypted backups cannot be opened without it (no reset exists). Make a new recovery key (`backup-key`) for future backups. |
| Restore | A damaged file, a wrong passphrase or a backup without a database is refused and leaves the current database untouched. To restore: stop `gateway` and `mdns`, run `docker compose run --rm -it --no-deps gateway spoolsteward-admin backup-restore /backups/<folder>/<file>`, then `docker compose up -d`. Check a file first with `backup-verify`. |

## 7. Portal language

| Symptom | Cause and fix |
|---|---|
| Pages are in English although you expected Italian | Order: user setting → browser language → `SPOOLSTEWARD_LANGUAGE` → English. Choose the language in *Settings*. |
| Some text is not translated | Texts from the printer (ink names, firmware, device warnings) or CUPS are shown as received. Audit reasons are stored in English on purpose. |
| Chinese characters look like boxes | The *browser's computer* has no CJK font installed. |
| Printed test or maintenance pages are English | By design: Russian and Chinese would need an embedded PDF font ([languages.md](languages.md)). |

## 8. Updates and certificates

| Symptom | Cause and fix |
|---|---|
| `scripts/update.sh` says `SPOOLSTEWARD_REGISTRY is empty` | The line `SPOOLSTEWARD_REGISTRY=` in `.env` lost its value. Write it back as in `docker/env.example` (`SPOOLSTEWARD_REGISTRY=ghcr.io/deh-aleh`) and run the script again. |
| `scripts/update.sh` says "This folder is a git clone" | The script updates a folder made from the release package. A git clone is a developer's copy and is not updated this way ([CONTRIBUTING.md](../../CONTRIBUTING.md)). |
| `refused: the portal only installs a version newer than …` | The portal asked for a version that is not newer than the installed one. The *Update* button never goes back; to go back, run `scripts/update.sh <version>` in a terminal. |
| `the package does not match its sha256 sum` | The downloaded file is not the one that was published (interrupted download, or a network that alters it). Nothing was changed: try again; if it persists, download it in the browser from the releases page. |
| `NOT healthy after the update: going back` | The new version did not become healthy within two minutes and the script put the previous files back. See `docker compose logs --tail 50 gateway`. |
| After the update neither the new nor the old version starts | The new one already changed the database. `update.sh` offers to restore the backup taken at the start (`YES`); by hand: `scripts/backup.sh restore data/backups/pre-update/<file>`. |
| The *Update* button said "rolled back" | The new version did not start and the old one runs again. Restoring the backup, if needed, is up to you (same command as above). |
| "Backup not done" notice in the bell | The network or USB disk of the backups is not mounted (or the backup failed for another reason, written in the notice). Mount it again, then *Run now* ([backup.md](backup.md)). |
| Certificate notice in the bell | See [certificates.md](certificates.md): each notice says what happened and what to do. |
| `scripts/cert-sync.sh` prints an error | The table of messages is in [certificates.md](certificates.md#54-if-something-goes-wrong). |
| File names in the audit show `[encrypted]` | `SPOOLSTEWARD_SECRET_KEY` was changed after they were saved. Put the previous key back in `.env` and run `docker compose up -d` ([data-protection.md](data-protection.md)). |

## 9. Still stuck

Gather `docker compose logs gateway`, `docker compose ps`, the output of `/healthz`, the gateway version
(`SPOOLSTEWARD_VERSION` in `.env`), and what the page showed, before asking for help. Never paste `.env` or the TLS key.
