# User guide

[Documentation index](../README.md) · English · [Italiano](../it/guida-utente.md)

For people who print and scan. Everything is at `https://<server>:8631/me` (administrators arrive at `/admin`; the menu
links to both).

## 1. Sign in

Open the portal and sign in with the account the administrator gave you (a local password), or with your office
credentials if the installation uses RADIUS. A temporary password must be changed at the first login.

## 2. My profile (`/me`)

- **Consumption**: jobs and pages used in the current week or month, your limit (including any extra quota granted to you),
  and when it resets.
- **Documentation**: the **?** icon in the top bar (`/docs`) opens this guide and the step-by-step guide for [Windows](windows.md). Administrators find the whole documentation there, with the same icon.
- **Print keys**: create a key for each device (give it a label). It is shown **once**: copy it at once (copy button). Use it
  as the password when the computer or phone asks for credentials; the user name is yours. A key only prints and scans — it
  never opens the portal and your domain password is never needed on a device. Revoke a key (or all of them) in *My keys* if a device is lost.
  When you create one you choose how long it lasts (from 1 month to 2 years, or one print only); RADIUS users have a shorter maximum and renew by signing in to the portal again. The bell at the top tells you when an administrator revokes, switches off or extends one of your keys.
- **Requests**: ask for more quota (temporary or permanent), for a change of group or for a permission (see below), and say why (a reason is optional unless the administrator requires one). The administrator
  approves, edits or rejects. You can have up to 3 open requests (the default limit), and cancel them.
- **Your permissions**: whether you may print in colour and one-sided, on which printers, and at which hours. If you may not print in
  colour, your prints come out in black and white; if you may not print one-sided, they come out double-sided. Scanning is not affected.
  Next to what you lack there is an **Ask** link (and **Ask for another printer**): choose what you need, write why, and an
  administrator answers. While a request waits, "(request waiting)" stands in place of the link. The answer may be permanent or last until a date; the bell tells you when it is approved, rejected or has
  ended, and the box shows the permissions you were granted and until when.

If a print is refused for quota, the message includes the link to ask for an exception. If it is refused because of your permissions
(a printer you may not use, a time outside your hours, or colour / one-sided when the installation refuses instead of correcting),
the message points to this page, and the bell tells you what happened and what to change. When a print is made in black and white or
double-sided because of your permissions, the bell tells you that too.

**Every refused or changed print gets its notice.** While a notice of the same kind is still open it is renewed instead of
repeated: its time is updated, it shows how many times it happened (×3) and the bell lights up again, so a program that retries does not fill the list.
This holds for prints refused because of your permissions, prints refused because you reached a quota limit
(weekly or monthly, jobs or pages: the notice says how much you used and that you can ask for more quota) and prints changed by your permissions.

**Secondary notices.** Some notices are only information about one print. They are marked as read by themselves after a while, so they do not
pile up. The secondary ones are the prints **forced** by your permissions (made in black and white, made double-sided). The delay is in *Settings* (`/me/settings`, "Secondary notices"): 20 minutes by default, counted from the last such
print; 0 means they stay until you dismiss them. A notice marked as read is not deleted: it is in the list under *Show dismissed too*.
All the other notices (refused prints, quota reached, keys, group, requests) stay until you dismiss them.

## 3. Print (`/me/print`)

Upload a PDF, photo (JPEG, PNG, GIF) or plain text. Choose the printer (only the ones you may use are listed), paper size, orientation, colour or black and white (only if you may print in colour), one-sided or double-sided (only if you may print one-sided and the printer has a duplex unit), a page
range (`1,5` or `2-4`), copies and an optional comment (50 characters, visible to administrators next to the file name). Same
quota and audit as every other print. Works from a phone with nothing installed. See [printing.md](printing.md) for connecting a
computer.

## 4. Scan (`/me/scan`)

Choose the scanner (if more than one), the source (automatic, glass, or feeder for all sheets), colour mode, quality, paper size,
format (PDF or JPEG) and a file name. You can leave the page while it scans and come back to download the file within 15 minutes.
See [scanning.md](scanning.md).

### Scanning from an app that cannot sign in

Most scan programs (NAPS2 and many others) cannot send a user name and password, so the gateway refuses them. If the button with
a scanner and a clock is at the top right of *Scan*, you can let your program in for a few minutes:

1. On the **computer where the scan program runs**, open *Scan* and press the button.
2. A box shows the address the server sees you at and a countdown (3 minutes unless the administrator changed it; 1 to 15 minutes are possible).
3. Start the scan in your program. Scans from that address are accepted as yours: your quota and permissions apply and they
   appear in the audit under your name. You can scan as often as you like until the time ends; a scan that has already
   started is delivered even if the time runs out. *Close the window* in the box ends it earlier.

If a scan comes from a different address it is refused, the audit records both addresses and your bell says that the window was for
one address and the request came from the other.

Limits, because the gateway can only compare addresses:

- **NAT**: behind a router shared with other devices (another network, VPN, guest Wi-Fi) the server may see the same address for
  every device, so during the window those devices can scan too. If browser and scan app are NATed differently, the addresses differ.
- **IPv4 and IPv6**: if the portal is reached over IPv6 and the scan app uses IPv4 (or the reverse), the addresses never match. Open
  the portal with the same kind of address the scan app uses.
- **Reverse proxy**: if you opened the portal through a proxy, the server sees the proxy's address, not your device's, and the scan
  app will be refused (or, if it goes through the proxy too, any device behind it could scan). Open the portal directly on the
  server's port (`https://<server>:8631`) before pressing the button. The box warns you when it detects a proxy.

## 5. Settings (`/me/settings`: click the logo and name at the top left)

- **Theme**: light, dark or automatic.
- **Language**: English, Italiano, Español, Русский, 中文. Saved for your account.
- **Auto-refresh** interval of the audit page (administrators); the Audit and Printers pages also have an *Automatic refresh* switch.
- **Secondary notices**: how long before the notices about a forced print are marked as read (20 minutes by default, 0 = never).
- **Change password** (local accounts).

## 6. Good to know

- Your quota counts accepted, completed and held jobs; refused, failed and canceled-before-printing jobs are free.
- A pages limit is checked before printing: a document that does not fit is refused at once.
- Printers that do not answer, or are blocked, are not offered. Printers you may not use are not offered either.
- If your hours are limited and it is outside them, the print page says when you can print, and a job sent anyway is refused.
