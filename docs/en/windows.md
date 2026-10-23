# Printing from Windows 11

[Documentation index](../README.md) · English · [Italiano](../it/windows.md)

Step by step guide to add a printer of the gateway to a Windows PC. Tested on Windows 11 Home 25H2: the printer was added at the first attempt, Windows asks for the credentials when you print and has a checkbox to remember them.

You need the **server name** (the portal address, for example `print.example.org`), your **user name** and a **print key**: not your account password, but a long text made for printing only.

## 1. Create a print key

1. Sign in to the portal (`https://<server>:8631/`) and open **My print keys** (`/me/keys`).
2. Create a key: choose how long it lasts and a label for this PC (for example "office PC").
3. The next page shows the key **only once**: copy it into a password manager before leaving. If you lose it, create another and revoke the old one.

## 2. Check that the PC trusts the server

A certificate from a public authority (for example Let's Encrypt) needs nothing. With a private authority or a self-signed certificate, install the certificate file your administrator gives you: double-click it → *Install Certificate* → *Local Machine* → *Trusted Root Certification Authorities*.
Without this Windows refuses the connection before sending anything, and the gateway log shows nothing.

## 3. Add the printer

1. *Settings → Bluetooth & devices → Printers & scanners → Add device*.
2. After a few seconds the gateway's printers appear as `<printer> @ <server name>`. Click one and press **Add**. If Windows says "Connecting…" for a long time, **wait up to 3-4 minutes**.

If no printer appears: restart the PC; if that is not enough, in *Printers & scanners* turn *Windows protected print mode* on and off and try again; if they never appear, the PC and the server are probably on different networks (VLANs) and the announcement does not cross: ask for an mDNS repeater, or add it by hand (§6).

## 4. Print and enter the credentials

Print from any program, choosing the printer. Windows asks for the **user name** (your portal user name) and the **password**: the **print key** of step 1, not the account password. Tick **Remember my credentials**; otherwise Windows asks at every print.

## 5. If the printer does not connect

If after 3-4 minutes it is still not ready, save the credentials before adding it:

1. *Control Panel → User Accounts → Credential Manager → Windows Credentials → Add a Windows credential*.
2. **Internet or network address**: the server name you use for the printer. **User name**: your portal user name. **Password**: the **print key**.
3. Remove the printer in *Printers & scanners* and repeat §3.

## 6. Adding by hand

*Add device → Add manually → Select a shared printer by name*, and paste `https://<server>:8631/printers/<queue>` (without `:8631` behind a proxy on port 443). Windows picks the "Microsoft IPP Class Driver" by itself. This way has not yet been tested all the way to a print.

## 7. Later

- **Key expired or revoked**: prints are refused with a login error. Create a new key (§1) and replace the saved one: *Credential Manager → Windows Credentials*, open the entry, change the password (from a terminal: `cmdkey /list`, `cmdkey /delete:<entry>`).
- **Printer renamed by the administrator**: the old entry stops working; remove it and add the new one (§3).
- **Print refused**: the bell (`/me/notices`) says why (quota, printer or hours not allowed…). From your profile you can ask for more quota or a permission ([user-guide.md](user-guide.md)).
