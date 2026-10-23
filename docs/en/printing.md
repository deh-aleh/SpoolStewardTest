# Printing and connecting devices

[Documentation index](../README.md) · English · [Italiano](../it/stampa.md)

There are two ways to print: through the **gateway's IPP address** (computers and apps) or from the **browser** (any phone, nothing to install). Both go through the same quota, audit and identity (`gw:<user>`).

## 1. Printing from the browser

*Print* in the menu (`/me/print`), signed in, no key needed.

- Accepted files: PDF, JPEG, PNG, GIF, plain text — recognised by their **content**. Maximum size `SPOOLSTEWARD_WEB_UPLOAD_MB` (25); copies up to `SPOOLSTEWARD_WEB_MAX_COPIES` (20).
- Options depend on the printer: paper size (those the queue lists), orientation, colour / black and white (if capable), **page range** (`1,5`, `2-4`, `3-`), copies and a **comment** (50 characters) shown under the file name in the audit. Pages counted for the limit are the selected pages × copies; a range beyond the end is refused before any quota is reserved.
- **Orientation.** *Automatic* follows the file: a PDF whose pages are already wide prints in landscape. *Landscape* on a PDF with portrait pages turns every page 90° and shrinks it to the sheet (the portal does it, because CUPS' PDF filters turn a portrait page upside down on some printers, for example Brother ones). It can not re-flow text the way LibreOffice
  does: for that, set landscape in the program and export the PDF again. Photos are turned by CUPS; plain text can not be turned (the portal says so and prints nothing).
- A printer that does not answer or is blocked is not offered; the order is in [printers.md](printers.md).
- **Permissions** ([quotas.md](quotas.md#5-advanced-permissions)): only the printers the user may use are listed; no colour choice without the colour permission; a **double-sided choice** (default / one-sided / long edge / short edge) appears when the printer has a duplex unit and the user may print one-sided (otherwise double-sided, long edge). The server decides with the
  same rules as the IPP gateway, so a form edited by hand gets the same result. Outside the allowed hours a banner says when printing is possible, and a job sent anyway is refused.

## 2. Printing with the IPP address

Address: `ipps://<server>:8631/printers/<queue>`. The client authenticates with HTTP Basic: **user name + print key** (created in *Profile*; [accounts.md](accounts.md)). The domain password does not print.

| Device | How to connect | Status |
|---|---|---|
| **Linux** (GNOME, KDE, CUPS) | **GNOME's Settings → Printers can not add a queue whose address carries a user name and a key**, so use the `lpadmin` command shown after "Create a key", or `scripts/add-printer-linux.sh <server> <queue> <user>` (asks for the key without echo, keeps it out of the shell history). Both store the key in the queue address (`/etc/cups/printers.conf`, readable only by root and group `cups`) and set `auth-info-required=none`. `ipps://user:key@server/printers/queue` can also be pasted in KDE (Add → URI) or the CUPS web interface (not tested). | ✅ tested with `lpadmin` and the script |
| **Any device with a browser** | `/me/print` | ✅ tested on Android |
| **Android + Mopria Print Service** | Sees the announced printers, asks for name and key per printer and remembers them. | ✅ tested (Android 13) |
| **Android system print service** | Sees the printers but can not ask for credentials: it gives up at the 401. | ❌ use Mopria or the browser |
| **iPhone / iPad** | The announcement carries the AirPrint subtype and `air=username,password`; whether iOS sends the credentials is unknown. | ⬜ not tested |
| **Windows 11** | "Add device" finds the announced printers; Windows asks for the credentials when you print, with a "remember" checkbox. Steps: [windows.md](windows.md). | ✅ tested on Windows 11 25H2 |
| **macOS** | Add a network printer with the IPP address and enter user name + key; "remember". | ⬜ not tested |

The device must trust the server's certificate ([certificates.md](certificates.md)). Printers announced by mDNS also appear on Linux desktops (Avahi) without a key: using them does nothing until the queue is added with the key in its address.

`scripts/print-test.sh SERVER[:PORT] QUEUE [USER]` sends one small text page (uses a sheet), for example `scripts/print-test.sh print.example.lan:8631 office-printer mario`; the user defaults to your login name, and `GATEWAY` can hold the server.

## 3. What the gateway does with each IPP operation

Allowed: print (`Print-Job`, `Create-Job` + `Send-Document`), cancel, list jobs and printers, capability queries. Forbidden for everybody: CUPS administrative operations, `Print-URI`, `Send-URI` (SSRF risk). Capability queries (`Get-Printer-Attributes`, `Validate-Job`) are answered **without credentials** because clients ask before they have any
(`SPOOLSTEWARD_ANONYMOUS_DISCOVERY=true`): they create no users or audit rows and forward nothing. Full path: [architecture.md](architecture.md#4-path-of-an-ipp-request). The gateway tells clients `uri-security-supported=tls` and `uri-authentication-supported=basic` (CUPS would say `none`): without it Mopria does not send credentials.

For `Print-Job`, `Create-Job` and `Validate-Job` the user's permissions apply after the blocked-printer check and before the quota. To correct a job the gateway rewrites `print-color-mode` and `sides` before CUPS and removes the PPD-style options `ColorModel`, `ColorMode` and `Duplex`; `Validate-Job` does not fail on colour or sides in `adjust` mode.
`CUPS-Get-Printers` and `CUPS-Get-Classes` leave out the printers a limited user may not use; a device that found the printer through the announcement still sees it, and the job is refused ([quotas.md](quotas.md#5-advanced-permissions)).

## 4. Announcement on the network (DNS-SD)

The `mdns` service announces every queue as `_ipps._tcp` with the `_universal` (AirPrint) subtype and TXT records `rp`, `pdl`, `URF`, `Color`, `Duplex`, `TLS`, `air`, refreshed every `SPOOLSTEWARD_MDNS_INTERVAL` seconds. The host name is `<name>.local` (Android resolves only those via multicast) and its address must be in the TLS certificate.
Blocked-hidden printers, unreachable devices and maintenance mode withdraw the announcement. Scanners are announced as `_uscans._tcp` ([scanning.md](scanning.md)).

## 5. Refused jobs, cancel and states

A refused job gives the client an IPP error with the reason and the portal address. A user can cancel their own jobs; an administrator can cancel queued jobs from the audit ("Cancel"), using the owner's identity. States are explained in [audit.md](audit.md).
