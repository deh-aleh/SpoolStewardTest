# Scanning

[Documentation index](../README.md) · English · [Italiano](../it/scansione.md)

Scanning uses **eSCL** (AirScan): `ipp-usb` serves it on the same port as IPP, so printing and scanning go through the same
USB device without fighting over it — **ipp-usb is the only owner** (the host's `ipp-usb` and SANE must be stopped). A print
sent during a scan works. SANE over the network was rejected: it does not authenticate per user and would bring back the
device conflict; TWAIN Direct is hardly supported.

## 1. Scanner discovery

Scanners are not configured one by one; they are **found at run time**:

- every network CUPS queue (ipp-usb gives each USB printer its own port) is asked for its eSCL capabilities;
- plus the addresses listed in `SPOOLSTEWARD_SCAN_URL` (`name=url`, comma separated; the image presets `http://ipp-usb:60000`;
  `none` = automatic discovery only).

Scanners answer eSCL on the queue's IPP port or on port 80; other cases go in `SPOOLSTEWARD_SCAN_URL`. A disconnected scanner
disappears within ~30 s. The `mdns` service announces each scanner as `_uscans._tcp` (`rs` = `eSCL` for the default one,
`eSCL-<id>` for the others).

## 2. Scanning from the portal

*Scan* (`/me/scan`): no installation, works from any phone.

- **Scanner** selection if there are several.
- **Source**: *Automatic* (feeder if the scanner declares `ScannerAdfLoaded`, otherwise glass), *Glass*, or *Feeder* (reads **all** sheets until
  `NextDocument` answers 404; maximum 100).
- Colour / grey / black and white; **quality** and **paper size** from what the scanner really declares for that source
  (for example a flatbed up to 1200 dpi and a single-sided feeder up to 300).
- Output: **PDF** (several sheets merged with pypdf) or **JPEG** (several sheets: ZIP); a **file name** field.
- Where to save: Chromium browsers open a folder-picker (File System Access API); others save to the downloads folder (a web
  page can not write to a hand-typed path).
- The scan runs as a **background task on the server**: you can leave the page and download later (the result is kept
  **15 minutes in memory**, visible only to its owner; one web scan at a time). If the scanner answers 409/503 (waking up)
  the gateway retries 3 times.

## 3. Scanning with eSCL clients

The gateway exposes `/eSCL/...`. `ScannerCapabilities` and `ScannerStatus` are public (like the IPP capability query);
everything else needs HTTP Basic with **user name + print key**, same authentication and throttling as printing. Allowed paths:
`ScanJobs` (POST), `ScanJobs/<id>/NextDocument|ScanImageInfo` (GET), `ScanJobs/<id>` (DELETE); the rest is 404.

- Every scan is a row in the audit with `kind=scan`; the device is the scanner's CUPS queue. States: `accepted` → `completed`
  (1 page per `NextDocument`), `failed` (scanner busy or not answering), `canceled` (only if canceled before delivering the page).
- A user can neither read nor cancel another user's scan (404). `Location` is rewritten to the gateway address.
- The eSCL job stays tied to the user until the client deletes it with DELETE — that is what frees the scanner.
- **Clients with no credentials** (NAPS2 can not send them): `SPOOLSTEWARD_ESCL_ANONYMOUS=true` accepts scans without login, recorded
  as the passwordless local account `unknown-guest` (it can not log in to the portal; the name is `SPOOLSTEWARD_ESCL_GUEST_USERNAME`), with the caller's IP in the audit
  options. Wrong credentials are still 401. While the option is off, a request without credentials is refused (401) and shows up as a notice in the bell ([admin-guide.md](admin-guide.md)), at most once per interval and source. **Default off**; anyone on the network who reaches port 8631 could scan: use only on a trusted
  network. NAPS2 did not find the mDNS announcement: type IP and port by hand.
### Scan window

**Scan window** (`SPOOLSTEWARD_ESCL_WINDOW`, on by default; a last resort, see its limits below): a button at the top right of *Scan* (a scanner with a clock) tells the
gateway "my scan app is about to scan". The gateway notes the address it sees for that connection and, for
`SPOOLSTEWARD_ESCL_WINDOW_SECONDS` (60–900, default 180), accepts requests **without credentials from that address only**, as
the person who pressed the button: their quota and permissions apply and the audit shows their name and the address. Any
number of scans can be made while the window is open. The window only decides whether a **new** scan may start: a scan that
has begun is delivered, and can be closed with DELETE, even if the window ends or is closed meanwhile (the job stays tied to
the address that started it). Windows live
in memory, so a restart closes them. A person has one window; if two people press from the same address, the last press wins.
With `SPOOLSTEWARD_ESCL_ANONYMOUS=true` the window changes only who gets the scan: the person, instead of `unknown-guest`. With
`SPOOLSTEWARD_ESCL_WINDOW=false` the button, the script and the texts are not in the page at all.
When a request without credentials comes from another address while a window is open, it is refused (401). The audit gets a
*denied* row for the window's owner with both addresses (the information icon shows them) and their bell says "scan refused:
the window was for X, the request comes from Y". The same pair is recorded at most once a minute, so a program that keeps
retrying does not fill the audit.

### Limits of the scan window

The scan window is a **last resort**, for apps that can not send a user name and a key at all and where you still want every scan
in the audit. It rests on one weak thing: the address of the TCP connection. Everything that changes that address breaks the method:

- **NAT**: if the browser and the scan app are behind the same router as seen by the server (for example another network, a VPN
  or a guest Wi-Fi), the server sees the router's address for *both*. The window then also accepts every other device behind
  that address, for as long as it is open. If the two are on different networks that are NATed differently, the addresses differ
  and the scan is refused.
- **IPv4 and IPv6 together**: a computer can reach the portal over IPv6 and the scan app can connect over IPv4 (or the reverse).
  The two addresses are different and do not match. Open the portal with the address of the same family that the scan app uses
  (the box that opens when you press the button says which address the server saw). An IPv4 address written in IPv6 form
  (`::ffff:192.168.1.5`) is treated as the IPv4 one.
- **Reverse proxy** (Nginx Proxy Manager and the like). If you open the portal *through* the proxy, the server sees the proxy's
  address, not your computer's, and the window is opened for the proxy's address. The scan app normally connects straight to
  port 8631 with its own address, so the two never match and the scan is refused. The reverse case is worse: if the scan app also
  goes through the proxy, every device that reaches the server through the proxy has the same address, so during the window
  *any of them* can scan. The server believes `X-Forwarded-For` only from `127.0.0.1` and from the addresses in `FORWARDED_ALLOW_IPS` ([installation.md](installation.md)), so a proxy on another machine does not help unless it is listed there.
  What works: press the button from a page opened **directly** on the gateway (`https://<server>:8631/me/scan`, not through the
  proxy). The box warns you when the request carried proxy headers.
- **A different device**: the button must be pressed from the computer that runs the scan app. Pressing it on a phone opens the
  window for the phone.
- **Changing address**: DHCP or Wi-Fi roaming during the window ends the match.

If the address in the box is not the one of the device that scans, the window can not work on that network: use
`SPOOLSTEWARD_ESCL_ANONYMOUS=true` on a trusted network, or a scan app that sends the user name and the print key. The user guide has
the same limits in short form for the people who press the button.

## 4. Notes

- A scan delivered to the client counts as completed even if the user does not save the file.
- If the feeder is asleep the scanner may declare it empty: *Automatic* then uses the glass (blank page). Choose *Feeder*, or
  remove and re-insert the sheets.
- Maintenance mode stops scanning too ([printers.md](printers.md)).
- Not tried yet: native eSCL clients that authenticate (sane-airscan, Image Capture).
