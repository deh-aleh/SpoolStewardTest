# Printing and connecting devices

[Documentation index](../README.md) · English · [Italiano](../it/stampa.md)

There are two ways to print: through the **gateway's IPP address** (computers and apps) or from the **browser**
(any phone, nothing to install). Both go through the same quota, audit and identity (`gw:<user>`).

## 1. Printing from the browser

*Print* in the menu (`/me/print`), signed in, no key needed.

- Accepted files: PDF, JPEG, PNG, GIF, plain text — recognised by their **content**, not by name or browser claim.
- Maximum size `SPOOLSTEWARD_WEB_UPLOAD_MB` (default 25); copies up to `SPOOLSTEWARD_WEB_MAX_COPIES` (default 20).
- Options depend on the selected printer: paper size (only those the queue lists), orientation, colour / black and white
  (if the queue is colour-capable), **page range** (`1,5`, `2-4`, `3-`), copies and a **comment** (max 50 characters) shown
  under the file name in the audit.
- **Orientation.** *Automatic* follows the file: a PDF whose pages are already wide prints in landscape by itself. *Landscape* on a
  PDF with portrait pages turns every such page 90° and shrinks it to the same sheet (the portal does it, because CUPS' PDF
  filters turn a portrait page upside down on some printers, for example Brother ones). It cannot re-flow the text as LibreOffice
  does when you change the page style to landscape: for that, set landscape in the program and export the PDF again. Photos are
  turned by CUPS. Plain text files cannot be turned (the portal says so and prints nothing).
- The pages counted for the pages limit are the selected pages × copies; a range beyond the end of the document is refused
  before any quota is reserved.
- A printer that does not answer is not offered; a blocked printer is not offered either. The list order is described in
  [printers.md](printers.md).
- **Permissions** ([quotas.md](quotas.md#5-advanced-permissions)). Only the printers the user may use are listed. The colour choice is
  hidden without the colour permission. A **double-sided choice** (printer default / one-sided / long edge / short edge) appears when the printer has a duplex unit
  and the user may print one-sided; a user who may not, prints double-sided (long edge). The form asks for a choice, the server decides
  with the same rules as the IPP gateway, so a form edited by hand gets the same result. Outside the allowed hours a banner at the top
  says when printing is possible, and a job sent anyway is refused.

## 2. Printing with the IPP address

Address: `ipps://<server>:8631/printers/<queue>`. The client authenticates with HTTP Basic: **user name + print key**
(created in *Profile*; see [accounts.md](accounts.md)). The domain password does not print.

| Device | How to connect | Status |
|---|---|---|
| **Linux** (GNOME, KDE, CUPS) | **GNOME's Settings → Printers can not add a queue whose address carries a user name and a key**, so use the `lpadmin` command shown on the page that appears after "Create a key" (it contains your user name and the key), or `scripts/add-printer-linux.sh <server> <queue> <user>`, which asks for the key without echo and keeps it out of the shell history. Both store the key in the queue address (`/etc/cups/printers.conf`, readable only by root and group `cups`) and set `auth-info-required=none`: the computer never asks again. The address `ipps://user:key@server/printers/queue` can also be pasted in KDE (System Settings → Printers → Add → URI) or in the CUPS web interface (`http://localhost:631`); these two are not tested by the project. | ✅ tested with `lpadmin` and the script |
| **Any device with a browser** | `/me/print` | ✅ tested on Android |
| **Android + Mopria Print Service** | Sees the announced printers, asks for name and key per printer and remembers them. | ✅ tested (Android 13) |
| **Android system print service** | Sees the printers but can not ask for credentials: it gives up at the 401. Manual add accepts only an IP, so the key can not be put in the address. | ❌ not usable — use Mopria or the browser |
| **iPhone / iPad** | The announcement carries the AirPrint subtype and `air=username,password`; whether iOS sends the credentials is unknown. | ⬜ not tested |
| **Windows 11** | "Add device" finds the announced printers and adds them at the first attempt; Windows asks for the credentials when you print, with a "remember" checkbox. Steps in the [Windows 11](windows.md) section below. | ✅ tested on Windows 11 25H2 |
| **macOS** | Add a network printer with the IPP address and enter user name + key; "remember". | ⬜ not tested |

The device must trust the server's certificate. Printers announced by mDNS also appear on Linux desktops (Avahi) without
a key: using them does nothing until the queue is added with the key in its address.

Scripts: `scripts/print-test.sh SERVER[:PORT] QUEUE [USER]` sends one small text page (uses a sheet), for example `scripts/print-test.sh print.example.lan:8631 office-printer mario`. Server and queue are required; the user defaults to your login name. You can set the server once in the `GATEWAY` environment variable and then pass only `QUEUE [USER]`.

### Windows 11

Full step-by-step guide: [windows.md](windows.md). In short: *Settings → Printers & scanners → Add device*, press *Add* on the announced printer, and at the first print enter your user name and the **print key** with *Remember my credentials* ticked. The PC must trust the server's certificate ([certificates.md](certificates.md)).

## 3. What the gateway does with each IPP operation

Allowed: print (`Print-Job`, `Create-Job` + `Send-Document`), cancel, list jobs and printers, capability queries.
Forbidden for everybody: CUPS administrative operations, `Print-URI`, `Send-URI` (SSRF risk). Capability queries
(`Get-Printer-Attributes` and `Validate-Job`, small bodies) are answered **without credentials** because clients ask before they have any
(`SPOOLSTEWARD_ANONYMOUS_DISCOVERY=true`): they create no users, no audit rows and forward nothing. The full path is in
[architecture.md](architecture.md#4-path-of-an-ipp-request).

The gateway tells clients `uri-security-supported=tls` and `uri-authentication-supported=basic` (CUPS would say `none`):
without it Mopria does not send credentials.

### Permissions and IPP

For `Print-Job`, `Create-Job` and `Validate-Job` the gateway applies the user's permissions (colour, one-sided, printers, hours)
after the blocked-printer check and before the quota. To correct a job it rewrites `print-color-mode` and `sides` in the request before it goes to
CUPS, and removes the PPD-style options `ColorModel`, `ColorMode` and `Duplex`. `Validate-Job` does not fail on colour or sides in `adjust`
mode. `CUPS-Get-Printers` and `CUPS-Get-Classes` leave out the printers a limited user may not use. A phone or computer that found the
printer through the network announcement still sees it, because the announcement is the same for everybody; the job is refused. Details and cautions:
[quotas.md](quotas.md#5-advanced-permissions).

## 4. Announcement on the network (DNS-SD)

The `mdns` service announces every queue as `_ipps._tcp` with the `_universal` (AirPrint) subtype and TXT records
`rp`, `pdl`, `URF`, `Color`, `Duplex`, `TLS`, `air`, refreshed every `SPOOLSTEWARD_MDNS_INTERVAL` seconds. The announced host
name is `<name>.local` (Android resolves only those via multicast), and its address must be in the TLS certificate.
Blocked-hidden printers, unreachable devices and maintenance mode withdraw the announcement. Scanners are announced as
`_uscans._tcp` ([scanning.md](scanning.md)).

## 5. Refused jobs, cancel and states

If a job is refused the client gets an IPP error with the reason and the portal address. A user can cancel their own
jobs; an administrator can cancel queued jobs from the audit ("Cancel"), using the owner's identity (no extra CUPS
privilege). States and their meaning are explained in the *Audit* page ([audit.md](audit.md)).
