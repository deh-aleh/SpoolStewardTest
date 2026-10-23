# Scanning

[Documentation index](../README.md) · English · [Italiano](../it/scansione.md)

Scanning uses **eSCL** (AirScan): `ipp-usb` serves it on the same port as IPP, so printing and scanning share the USB device without fighting over it — **ipp-usb is the only owner** (the host's `ipp-usb` and SANE must be stopped). A print sent during a scan works. SANE over the
network was rejected: it does not authenticate per user and would bring back the device conflict.

## 1. Scanner discovery

Scanners are **found at run time**: every network CUPS queue is asked for its eSCL capabilities (ipp-usb gives each USB printer its own port), plus the addresses in `SPOOLSTEWARD_SCAN_URL` (`name=url`, comma separated; the image presets `http://ipp-usb:60000`; `none` = automatic discovery only).
Scanners answer eSCL on the queue's IPP port or on port 80; other cases go in `SPOOLSTEWARD_SCAN_URL`. A disconnected scanner disappears within ~30 s. The `mdns` service announces each one as `_uscans._tcp`.

## 2. Scanning from the portal

*Scan* (`/me/scan`): no installation, works from any phone.

- **Scanner** selection if there are several. **Source**: *Automatic* (feeder if the scanner declares it loaded, otherwise glass), *Glass*, or *Feeder* (reads **all** sheets, maximum 100).
- Colour / grey / black and white; **quality** and **paper size** from what the scanner declares for that source.
- Output: **PDF** (sheets merged) or **JPEG** (several sheets: ZIP); a **file name** field. Chromium browsers open a folder picker; others save to the downloads folder.
- The scan runs as a **background task on the server**: you can leave the page and download later (one web scan at a time). If the scanner answers 409/503 (waking up) the gateway retries 3 times.
- Finished scans wait **in the server's memory**, visible only to their owner (a colour page at 300 dpi is about 1 MB; a 100-page feeder 100 MB). Three settings keep it under control: `SPOOLSTEWARD_SCAN_KEEP_MINUTES` (10), `SPOOLSTEWARD_SCAN_KEEP_MB` (50, all people together:
  the **oldest finished ones are dropped first**, the newest is always kept) and `SPOOLSTEWARD_SCAN_DELETE_AFTER_DOWNLOAD` (`true`: a scan can be downloaded once). A scan that has been dropped must be scanned again: download it soon.

![A scan from the glass in progress: the Scan button is greyed out and the bar under it says that the scan is running](../images/en/scan-running.png)

## 3. Scanning with eSCL clients

The gateway exposes `/eSCL/...`. `ScannerCapabilities` and `ScannerStatus` are public; everything else needs HTTP Basic with **user name + print key**, with the same authentication and throttling as printing. Allowed paths: `ScanJobs` (POST), `ScanJobs/<id>/NextDocument|ScanImageInfo` (GET),
`ScanJobs/<id>` (DELETE); the rest is 404.

- Every scan is an audit row with `kind=scan`; the device is the scanner's CUPS queue. States: `accepted` → `completed` (1 page per `NextDocument`), `failed` (scanner busy or not answering), `canceled` (only before delivering the page).
- A user can neither read nor cancel another user's scan (404). The eSCL job stays tied to the user until the client deletes it with DELETE — that frees the scanner.
- **Clients with no credentials** (NAPS2 can not send them): `SPOOLSTEWARD_ESCL_ANONYMOUS=true` accepts scans without login, recorded as the passwordless local account `unknown-guest` (name in `SPOOLSTEWARD_ESCL_GUEST_USERNAME`; it can not log in to the portal) with the caller's IP in the audit.
  Wrong credentials are still 401. While the option is off, a request without credentials is refused (401) and shows up as a notice ([admin-guide.md](admin-guide.md)). **Default off**: anyone on the network who reaches port 8631 could scan, so use it only on a trusted network. NAPS2 did not find the mDNS announcement: type IP and port by hand.

### Scan window

**Scan window** (`SPOOLSTEWARD_ESCL_WINDOW`, on by default; a last resort, see its limits below): a button at the top right of *Scan* tells the gateway "my scan app is about to scan". The gateway notes the address it sees for that connection and, for `SPOOLSTEWARD_ESCL_WINDOW_SECONDS` (60–900, default 180),
accepts requests **without credentials from that address only**, as the person who pressed it: their quota and permissions apply and the audit shows their name and the address. Any number of scans can be made while it is open.

The window only decides whether a **new** scan may start: a scan that has begun is delivered, and can be closed with DELETE, even if the window ends. Windows live in memory, so a restart closes them. A person has one window; if two people press from the same address, the last press wins. With
`SPOOLSTEWARD_ESCL_ANONYMOUS=true` the window changes only who gets the scan (the person instead of `unknown-guest`). With `SPOOLSTEWARD_ESCL_WINDOW=false` the button is not in the page at all.

When a request without credentials comes from another address while a window is open, it is refused (401). The audit gets a *denied* row for the window's owner with both addresses, and their bell says "scan refused: the window was for X, the request comes from Y" (at most once a minute per pair).

### Limits of the scan window

The window is a **last resort**, for apps that can not send a user name and a key at all and where you still want every scan in the audit. It rests on one weak thing, the address of the TCP connection; whatever changes that address breaks it:

- **NAT**: if the browser and the scan app are behind the same router as seen by the server (another network, a VPN, a guest Wi-Fi), the server sees the router's address for *both*, and the window also accepts every other device behind that address while it is open. If they are NATed
  differently the addresses differ and the scan is refused.
- **IPv4 and IPv6 together**: if the computer reaches the portal over IPv6 and the scan app connects over IPv4 (or the reverse), the addresses do not match. Open the portal with the address family the scan app uses (the box that opens when you press the button says which address the server saw). `::ffff:192.168.1.5` counts as IPv4.
- **Reverse proxy**: if you open the portal *through* the proxy, the window is opened for the proxy's address, while the scan app normally connects straight to port 8631 with its own, so the scan is refused. Worse, if the scan app also goes through the proxy, *any* device behind it can scan during the window.
  The server believes `X-Forwarded-For` only from `127.0.0.1` and from `FORWARDED_ALLOW_IPS` ([certificates.md](certificates.md#41-behind-a-reverse-proxy)). What works: press the button from a page opened **directly** on the gateway (`https://<server>:8631/me/scan`); the box warns you when the request carried proxy headers.
- **A different device**: press the button on the computer that runs the scan app, not on a phone.
- **Changing address** (DHCP, Wi-Fi roaming) during the window ends the match.

If the address in the box is not the scanning device's, the window can not work on that network: use `SPOOLSTEWARD_ESCL_ANONYMOUS=true` on a trusted network, or a scan app that sends the user name and the print key. The user guide repeats this in short.

## 4. Notes

- A scan delivered to the client counts as completed even if the user does not save the file.
- If the feeder is asleep the scanner may declare it empty: *Automatic* then uses the glass (blank page). Choose *Feeder*, or remove and re-insert the sheets.
- Maintenance mode stops scanning too ([printers.md](printers.md)). Not tried yet: native eSCL clients that authenticate (sane-airscan, Image Capture).
