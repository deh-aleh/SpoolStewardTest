# Accounts, groups and print keys

[Documentation index](../README.md) · English · [Italiano](../it/account.md)

## 1. Who is who

Every person is **one row** in the `user` table with a *provider*: **local** (the argon2id password lives in the gateway database and is managed in the portal) or **RADIUS** (optional; the office server checks the password, the gateway does not store it).

**User names are unique across all sources.** CUPS and the audit only receive `gw:<name>`, so two people with the same name would share identity and quota. The *row* decides the login method: a local account can not be opened by RADIUS even if RADIUS knows the same name. An unknown RADIUS user is
created at the first portal login (in the default group) when `SPOOLSTEWARD_AUTO_CREATE_USERS=true`; if the name is already local, no. RADIUS names are matched case-insensitively (`Mario` and `mario` are one person). With `SPOOLSTEWARD_RADIUS_HOST` and `SPOOLSTEWARD_RADIUS_SECRET` both `none` only local accounts exist; if RADIUS does not answer, access is denied.

## 2. The first administrator

`administrator` exists without a password. At the first start the gateway prints a **one-time code** (`docker compose logs gateway | grep "FIRST RUN"`); `/setup` asks for it and for the new password, then deactivates itself. Alternative: `spoolsteward-admin set-admin-password`. There is deliberately no page open to everybody
for becoming an administrator.

The emergency account can not be disabled, demoted, deleted or have its source changed, and its name can not be given to another account. It is the only account without a group: no quota, no permission rule, and it can not ask to change group. The other administrators live in the "administrator" group, whose limits and permissions you can edit (the group can not be deleted).
Nobody can disable, demote or delete themselves, nor the last administrator.

## 3. Passwords (local accounts)

Minimum 12 characters, not equal to the user name. A password created or reset by an administrator is **temporary**: shown once, changed at first login. Changing a password closes the user's other sessions. Login always spends the cost of a hash, even for names that do not exist, and never reveals whether a name exists.

## 4. Groups

Quota limits are set per **group**, with optional per-user overrides ([quotas.md](quotas.md)). Two system groups always exist and can not be deleted:

- `default` — where everybody starts and where users go when their group is deleted or they stop being administrators;
- `administrator` — only for administrators: promoting a user moves them there, demoting moves them back to `default`. A non-administrator can not be moved into it by hand.

A group can be marked **requestable**: users may then ask to move into it. Administrative groups never are.

## 5. Print keys

Operating systems keep print passwords in clear text (for example `printers.conf` on Linux), so clients **never** use the domain password: they use a **personal print key**.

- Created by the user in *Profile → Create a key* (a label per device). Shown **once**; stored only as a sha256 hash of 192 random bits. Valid **only for printing and scanning**; it never opens the portal.
- **The user chooses how long it lasts**: 1, 3 or 6 months, **1 year (suggested)**, 1 year and 6 months, 2 years, or **one print only**. The longest is `SPOOLSTEWARD_KEY_TTL_DAYS_RADIUS` (90 days) for RADIUS users and `SPOOLSTEWARD_KEY_TTL_DAYS_LOCAL` (730) for local ones; the menu shows only what fits. A RADIUS user has to log in again for another key,
  so someone who leaves the company stops printing at the expiry.
- **Single-use key**: valid for one print. If nobody uses it, it stops after `SPOOLSTEWARD_KEY_ONCE_HOURS` hours (24). Once its print is accepted the key leaves the list; for `SPOOLSTEWARD_KEY_ONCE_GRACE_SECONDS` seconds (120) it can still ask how the job is going, not print another. A print refused by the quota does not spend it; one that was accepted and then failed does.
- A key is personal: whoever holds it prints and scans as that user, on that user's quota and audit. If it was passed on, or the device is lost, revoke it. Last use (time and IP) is recorded at most once a minute.
- Revoke one key or all in *My keys* (`/me/keys`) or, for an administrator, in `/admin/users/<id>/keys`. An administrator can also **switch a key off** (its expiry keeps running; it can be switched on again), **extend it** (new expiry = now + the chosen duration, within the account's maximum; the time left is **not** added) or **revoke it**.
  Every action goes to the audit (`key.disable`, `key.enable`, `key.extend`, `key.revoke`) and **the user finds it in their notices** (`/me/notices`), without the administrator's name; none when an administrator acts on their own key.
- The user also finds in their notices when an administrator **changes their group** (also when a group is deleted and its members move to `default`), **makes them administrator or removes the right**, **gives or withdraws extra quota**, or when a **request is approved or rejected** (with the note). Nothing is said about changes you make to yourself,
  and changing a group's limits or using `set-group` sends none.
- **Revoked** and **expired** keys disappear `SPOOLSTEWARD_KEY_HISTORY_DAYS` days later (14; 0 = never); the list says when. Optional, off by default: `SPOOLSTEWARD_KEY_EXPIRY_WARN_ENABLED=true` warns in the bell `SPOOLSTEWARD_KEY_EXPIRY_WARN_DAYS` days (7) before expiry, once per key.

`SPOOLSTEWARD_IPP_AUTH=keys` (default) accepts only keys for printing. `keys+radius` also accepts the RADIUS password for RADIUS users during a transition (never for local accounts): it encourages saving the domain password on clients, so use it only temporarily. Whatever starts with `cpk-` is taken for a print key: if it is not valid
(expired, revoked, switched off) it is refused at once and **never sent to RADIUS**, so an old key left on a phone can not add failed logins to the domain account. The only consequence: a domain password that begins with `cpk-` can not be used for printing.

## 6. Protection of logins

- 5 *different* wrong passwords within 5 minutes for the same (user, IP) block that pair for 5 minutes, plus a per-IP cap. The same wrong password repeated by one client counts once (libcups retries 3–5 times), and a stranger can not lock another user out. The lock is kept in `/data/login-lockout.json`
  (user names, addresses and times only), so a restart does not clear it; it does **not** disable the account, and the same user from another address is not affected. While blocked, the login page answers "Too many failed attempts, retry later" even if the password is then right.
- Disabling a user in the portal blocks portal login and keys immediately.
- Portal sessions: signed, 8 hours, CSRF token on every POST, `Secure` + `SameSite=strict` cookies; pages that show a secret are sent with `Cache-Control: no-store`.

## 7. Deleting a user

Removes the account, keys, requests, grants (quota and permission), personal limits and permissions, but **not the history**: audit rows keep the user name and only lose the link to the user id. The administrator types the user name to confirm.

## 8. The source of an account

The user's edit page shows where the account signs in (local or RADIUS) as plain text; it can not be changed from the portal. To move someone from one source to the other, delete the account and create it again on the other side (the print history keeps the user name).
