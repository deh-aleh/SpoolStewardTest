# Known limitations

[Documentation index](../README.md) · English · [Italiano](../it/limiti.md)

What SpoolSteward does **not** do (yet) or does with caveats. The short version is in the [README](../../README.md#limits).

## 1. Installation

- A **Linux host** is required for USB printers and for mDNS (on Docker Desktop for Windows/macOS only network printers work and nothing is announced).
- One gateway per USB printer (`ipp-usb` needs exclusive use); its container sees every USB device of the host (needed for hot-plug).
- The database is SQLite (one file): one gateway, no high availability, no external database. Published images exist for amd64 and arm64 only.
- `scripts/update.sh` replaces `docker-compose.yml` and the scripts, never `.env`, `config/` or `data/`, and does not work in a folder cloned with git. New options of a version are not written into your `.env`: they work with their default (compare with `docker/env.example`).
- The *Update* button needs the optional `updater` container, which holds the Docker socket (root on the host) and is off by default. It updates only inside the same major version and, if the new version does not start, puts the old one back without restoring the backup.
- Backups on a network share: file permissions are decided by the mount (use encryption). An unmounted share receives no backup: "write only if mounted" is on by default for portal backups, off for the *General* one ([backup.md](backup.md)).
- Going back to an older version after a database migration requires restoring a backup (`scripts/update.sh`).
- CUPS keeps the names of its latest jobs (up to 2,000) in its own volume: the button and the retention of names do not touch them.
- Certificates: devices must trust the company CA; the announcement uses the IP, so the certificate must list it ([certificates.md](certificates.md)).

## 2. Quota

- The **pages limit is soft** for files whose pages can not be counted: unknown formats, encrypted PDFs, files over 64 MB. The job that crosses the limit is printed; its real pages then block the following ones.
- With `Create-Job` + `Send-Document` the count arrives after the job was accepted. A `Create-Job` with no document frees its quota only when CUPS cancels it (default 3 h). Page counting applies only when a pages limit exists and the document is readable.
- If RADIUS tells `Mario` from `mario`, for the gateway they are one person (same quota).
- Limits for paper size do not exist. Colour, one-sided, printers and hours do ([quotas.md](quotas.md#5-advanced-permissions)), with these limits:
  - **Forced black and white may not work** with some printers and drivers, above all with images a phone has already turned into colour pixels (iPhone / AirPrint). For a guarantee use `SPOOLSTEWARD_PERMISSION_MODE=refuse`; double-sided uses the standard `sides` attribute and is more reliable if the queue declares its duplex unit.
  - With `refuse`, Windows and Android often refuse many jobs, because they send one-sided or colour in every job. The default `adjust` avoids it.
  - The mDNS announcement is the same for everybody: printers a person may not use stay visible and the job is refused. The printer list given after the login hides them.

## 3. Clients

- **Android's system print service can not authenticate**: use Mopria Print Service, or the browser. Android keeps credentials per printer and per session; the gateway can only revoke the key.
- iPhone/iPad and macOS have **not been tested**; printing works from Windows 11, but whether the file name arrives in `job-name` is not verified.
- Print keys of Linux/CUPS clients end up saved on the client (revocable and useless elsewhere). Announced printers appear on Linux desktops (Avahi) without a key and do nothing until the queue is added with it. **GNOME** can not add a queue whose address carries a user name and a key: use `lpadmin` or `scripts/add-printer-linux.sh`.
- A single-use key is spent when its print is accepted: if CUPS or the printer then fail, another key is needed. It still works for 2 minutes, only to ask for the status.
- The **scan window** is a last resort: it compares only the address of the TCP connection, so NAT, a reverse proxy, IPv4/IPv6 mixes or a changing address break it ([scanning.md](scanning.md#limits-of-the-scan-window)).

## 4. Printers and scanners

- Automatic USB discovery: a printer served by `ipp-usb` is never taken through CUPS' usb backend; printers without IPP-over-USB need a driver in the `cups` image (brlaser, gutenprint, splix, foo2zjs). Coexistence of both ways on one USB bus with several printers has been tested with one device at a time. Tested so far: HP Smart Tank 7300 (ipp-usb) and Brother DCP-7055 (usb backend, brlaser).
- USB queues unreachable for 8 h (configurable) are removed by themselves; network queues never are. A queue of `CUPS_QUEUES` is created once: if you delete it, it stays deleted (`scripts/queue-restore.sh NAME --reboot`).
- Scanners are found automatically only if they answer eSCL on the queue's IPP port or on port 80; otherwise `SPOOLSTEWARD_SCAN_URL`. Native eSCL clients that authenticate have not been tried (sane-airscan, Image Capture). A scan delivered to the client counts as completed even if it is not saved. A sleeping feeder may be declared empty: *Automatic* then uses the glass.
- Lifetime counters come from HP's web service only. Pages printed by the gateway (test, maintenance) are always in English. If the gateway is off, a missed maintenance interval is not made up.
- Browser print, *Landscape*: PDFs with portrait pages are turned and shrunk (text is not re-flowed); plain text can not be turned; photos are turned by CUPS ([printing.md](printing.md)).

## 5. Backup and security

- Built-in backups: SQLite only; CUPS queues not included (they are in `scripts/backup.sh`); configuration files are restored by hand; no direct remote destinations; the passphrase can not be recovered. `scripts/backup.sh restore` is tested by hand only.
- The audit and the database are not encrypted by default (see [data-protection.md](data-protection.md)).

## 6. Languages

Spanish, Russian and Chinese were written without native review. Dates and numbers are not localised. The audit's stored text and the server logs stay English.

## 7. Feature status

What has been tested and what is missing is in the [README](../../README.md#status).
