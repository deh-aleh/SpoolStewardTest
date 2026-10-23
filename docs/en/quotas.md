# Quotas, requests and grants

[Documentation index](../README.md) · English · [Italiano](../it/quote.md)

## 1. Limits

A **rule** says: *this user or group may print at most N **jobs** (or **pages**) per **week** (or **month**)*.

- Rules belong to a group, with optional **per-user overrides**: a user rule replaces the group's for the same metric.
- Periods: **weekly** (Monday–Sunday) or **monthly** (calendar month), in `SPOOLSTEWARD_TIMEZONE`, daylight saving included. No rule = no limit.
- Where to set them: *Groups* → edit, *Users* → edit, or the CLI: `spoolsteward-admin group-limit staff 20 --period monthly`,
  `spoolsteward-admin user-limit mario 5 --metric pages` (`none` removes a limit).

### What counts

Jobs `accepted`, `completed` or `held` count. **Rejected, failed and canceled-before-printing jobs do not consume quota.** Users see consumption and the next reset in *Profile*.

### Counting pages before printing

With a pages limit, the gateway reads the document **before** printing (up to 64 MB) and counts its pages: PDF, JPEG/PNG/GIF = 1, URF from the header, PostScript
from `%%Pages`, plain text ≈ 60 lines per page; times the copies and the page range. If `used + pages > limit` the job is **refused at once**:

> `would be exceeded (used 90, this document 200, limit 100)`

Otherwise the pages are recorded immediately, so simultaneous jobs see them (checking and recording are one locked step, active grants included). After printing, the
reconciler replaces the estimate with the **real pages** read from CUPS. For files whose pages can not be counted the limit is soft: [limitations.md](limitations.md#2-quota).

## 2. Requests from users

In *Profile* a user can ask for:

1. **More quota** — metric, amount, **temporary** or **permanent**, and a reason. Only metrics that have a limit. The **period is the rule's**, shown read-only ("your pages limit is monthly: the exception lasts until 1 November").
2. **Change of group** — only towards groups marked *requestable*.
3. **A permission** — colour, one-sided, one more printer, or printing outside the hours ([Asking for a permission](#asking-for-a-permission)).

At most `SPOOLSTEWARD_MAX_OPEN_REQUESTS` (3) requests open per user; the reason is optional unless `SPOOLSTEWARD_REQUEST_REASON_REQUIRED=true`; requests expire after
`SPOOLSTEWARD_REQUEST_TTL_DAYS` (14). The user can cancel a pending request.

## 3. Approval and grants

Administrators see pending requests in **Requests** (badge in the menu) and **approve, edit or reject** them; they can change amount and type (even make a permanent
request temporary) and add a note. Approval produces a **grant** that is **added** to the limit: `effective limit = rule + active grants`.

- A **temporary** grant lasts until the end of the period window **in which the approval happens** (a request made on Sunday night and approved on Monday must not be born expired).
- A **permanent** grant applies to every following period until revoked.
- A group change moves the user at once. Approving twice grants once. `job_log` is never edited.

Administrators can also grant or revoke quota directly in **Requests**; everything lands in the administrators' log ([audit.md](audit.md)). The users list shows active grants as a coloured `+N`.

## 4. Reasons shown to users

A refused job gives the IPP client the reason in English plus the portal address to ask for an exception. The portal shows the same sentence translated ([languages.md](languages.md));
the audit stores the English one.

## 5. Advanced permissions

Quotas say *how much*; permissions say *what*, *where* and *when*. They are set with the **Advanced permissions** button at the bottom of the group page (*Groups* → edit) and of the user page (*Users* → edit); the block is closed when everything is at the default.

![The edit page of a group: name, job and page limits, period, requestable, and the open Advanced permissions block with colour allowed and one-sided not allowed](../images/en/admin-group-edit.png)

| Permission | If it is not given |
|---|---|
| **Colour** | Prints come out in black and white. |
| **One-sided** | Prints come out double-sided (long edge), on printers with a duplex unit. |
| **Printers** | Only the printers in the list can be used. Default: all. |
| **Hours** | Printing works only on the chosen days between the two times. Default: any time. |

Everything is allowed by default. Each permission is a row of buttons (*allowed* / *not allowed*, *all* / *only some*, *any time* / *some hours*); the printers, days and times appear only after the
second choice. In a user's page every row starts with *as the group (…)*.

**Group and user.** The group sets the values; each field of a user is "as the group" (default) or has its own value, field by field. Changing a group's permissions changes every user who follows it
without a notice; changing a user's own permissions sends one. An administrator is limited like anybody else, except the **emergency administrator**
(`SPOOLSTEWARD_BOOTSTRAP_ADMIN_USERNAME`, a local account): it has every permission (its page shows the block off and the server ignores anything posted for it), no group, no quota, and can not ask to change group.
**Scanning is not limited** by any of this.

### Printers

The list holds queue names in lower case. A printer added **after** the list was made is **not** allowed until you tick it. If a queue is **renamed** ([printers.md](printers.md)), its name changes in every list by itself.
If a queue is **removed**, its name stays in the lists ("not present"), so a replacement given that name finds the permissions again.

### Hours

One window: days of the week and two times (`HH:MM`) in `SPOOLSTEWARD_TIMEZONE`. The end is not included: `08:00` to `17:00` allows 16:59, not 17:00. If the end is earlier than the start (`22:00` to `06:00`) the
window goes past midnight and belongs to the day it starts. Start and end can not be equal. Hours are checked **when the job is sent**: an accepted job is not stopped when the window closes.

### What happens to a job

The gateway checks, in order: blocked printer or maintenance, then permissions, then quota.

| What the client asked | Not allowed | Result |
|---|---|---|
| A printer not in the list | | **Refused**, always |
| A time outside the hours | | **Refused**, always |
| Colour not stated (nothing, or `auto`) | no colour | Printed in black and white. Nobody is told |
| Colour stated (`color`) | no colour | `SPOOLSTEWARD_PERMISSION_MODE=adjust` (default): printed in black and white, the user gets a notice. `refuse`: refused |
| Sides not stated | no one-sided | Printed double-sided (long edge). Nobody is told |
| `one-sided` stated | no one-sided | `adjust`: printed double-sided, notice. `refuse`: refused |

The rules look at `print-color-mode`, `sides` and the options some drivers send by PPD names (`ColorModel`, `ColorMode`, `Duplex`). A printer with no colour is never touched by the colour rule; one without
a duplex unit prints one-sided and is not refused. The gateway asks the queue what it can do (remembered a minute); if CUPS does not answer, only what the client stated explicitly is corrected.

A refused job is audited as `denied` with the reason, uses **no quota and no pages**, and the user gets a notice (each refusal renews the open one with a counter). The IPP client receives the reason and the address of
the profile page, where the user sees their permissions and can ask for the missing one.

### Read this before choosing the mode

1. **Windows and Android often send `sides=one-sided` or the colour in every job**, even if the person never touched the option. With `refuse`, many of their jobs are refused until they change the option in the print
   dialog. That is why the default is `adjust`.
2. **Forced black and white may not work** with some printers and drivers, above all with images a phone has already turned into colour pixels (iPhone / AirPrint): the gateway asks, the printer decides. For a guarantee use
   `refuse`. Double-sided uses the standard `sides` attribute and is more reliable, as long as the queue declares its duplex unit.
3. **The mDNS announcement is the same for everybody**: printers a person may not use stay **visible** and a job sent to them is refused. The printer list the gateway gives after the login leaves them out.
4. Hours are checked when the job is sent; scanning is not limited; the emergency administrator is exempt from permissions and quotas.

### What the user sees

- *Profile*: the box "Your permissions" with the link to ask for what is missing. Printer lists and ready-made addresses show only the printers the person may use.
- *Print*: only allowed printers; no colour choice without the colour permission; a double-sided choice on duplex printers only for those who may print one-sided; a banner with the allowed hours when outside them.
- The bell: "refused", "quota reached" and "changed" notices (the "changed" one is *secondary*: read automatically after the time chosen in `/me/settings`, 20 minutes by default), and one when an administrator changes the person's own permissions.

The administrators' log (*Actions*) records every change to permissions with the old and new values.

### Asking for a permission

A person who lacks a permission asks in the box "Your permissions" of their profile: **Ask** next to colour, one-sided and hours, **Ask for another printer** for printers. The form offers only what the person lacks, and
only printers that exist now. A reason is required only if `SPOOLSTEWARD_REQUEST_REASON_REQUIRED=true` (500 characters at most). A request is refused if the person already has that permission or has an unanswered one for the
same thing. The same brakes as quota requests apply (`SPOOLSTEWARD_MAX_OPEN_REQUESTS` for all kinds together, `SPOOLSTEWARD_REQUEST_TTL_DAYS`).

The administrator sees it in **Requests** and chooses **permanent** or **until the end of** a date (counted through the whole day in `SPOOLSTEWARD_TIMEZONE`, today or later). Approving creates a *permission grant* that
**widens** what the group and the person's rule give and never takes anything away:

| Asked | The grant gives |
|---|---|
| Colour | colour is allowed |
| One-sided | one-sided printing is allowed |
| A printer | that printer is added to the person's list (with no list every printer is allowed already) |
| Outside the hours | the hours window does not apply |

Approving twice grants once; a refused approval (for example a past date) leaves the request waiting. Active permission grants appear in the same *Active grants* list as the quota ones, with *Revoke*; ended ones go to
the history, and *Delete ended grants* removes both kinds. The person's edit page lists their active permission grants. The administrators' log records each approval, direct grant, revocation and deletion.

An administrator can also grant a permission without a request (*Grant without a request* → **a permission**). Once a user is chosen, the form offers only what that user lacks; the server refuses what they already have.
Without JavaScript the form shows all fields and the server reads only those of the chosen kind.

The bell tells the person when the request is approved (with the end date) or rejected, when an administrator grants or revokes a permission, and, within the hour after the end date, that a temporary permission ended.
An administrator is never told about what they did to their own account.
