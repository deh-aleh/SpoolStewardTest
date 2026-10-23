# Quotas, requests and grants

[Documentation index](../README.md) · English · [Italiano](../it/quote.md)

## 1. Limits

A **rule** says: *this user or this group may print at most N **jobs** (or **pages**) per **week** (or **month**)*.

- Rules belong to a group, with optional **per-user overrides**. A user rule replaces the group's rule for the same metric.
- Metrics: **jobs** and **pages**. Periods: **weekly** (Monday–Sunday) or **monthly** (calendar month), in the time zone
  `SPOOLSTEWARD_TIMEZONE`, also across the daylight-saving change.
- No rule = no limit.
- Where to set them: *Groups* → edit (group limits), *Users* → edit (personal limits), or the CLI:
  `spoolsteward-admin group-limit staff 20 --period monthly`, `spoolsteward-admin user-limit mario 5 --metric pages`
  (`none` removes a limit).

### What counts

Jobs in the state `accepted`, `completed` or `held` count. **Rejected, failed and canceled-before-printing jobs do not
consume quota.** The user sees consumption and the next reset in *Profile*.

### Counting pages before printing

When a pages limit applies to a user, the gateway reads the document **before** printing (up to 64 MB) and counts its pages:
PDF (pypdf), JPEG/PNG/GIF = 1, URF from the header, PostScript from `%%Pages`, plain text ≈ 60 lines per page; then
multiplies by the copies and by the selected page range. If `used + pages > limit` the job is **refused at once**:

> `would be exceeded (used 90, this document 200, limit 100)`

Otherwise the pages are recorded immediately, so simultaneous jobs see them. After printing, the reconciler replaces the
estimate with the **real pages** read from CUPS. A job whose page count is not yet known counts as at least 1 page.

### The soft limit

For files whose pages cannot be counted, and in a few other cases, the pages limit is soft: [limitations.md](limitations.md#2-quota).

### Atomic reservation

Checking the limit and recording the job happen in one locked step, so many simultaneous jobs can not exceed the limit. Active grants are added to the limit **inside** the same check.

## 2. Requests from users

In *Profile* a user can ask for:

1. **More quota** — choose the metric, the amount, **temporary** or **permanent**, and write a reason. The user can ask only
   for metrics that actually have a limit. The **period is not chosen by the user**: it is that of the rule the
   administrator set; the form shows it read-only ("your pages limit is monthly: the exception lasts until 1 November").
2. **Change of group** — only towards groups marked *requestable*.
3. **A permission** — colour, one-sided printing, one more printer or printing outside the hours (see [Asking for a permission](#asking-for-a-permission)).

Limits on requests: at most `SPOOLSTEWARD_MAX_OPEN_REQUESTS` (3) open requests per user, the reason is optional unless `SPOOLSTEWARD_REQUEST_REASON_REQUIRED=true` (always treated as plain text),
requests expire after `SPOOLSTEWARD_REQUEST_TTL_DAYS` (14). The user can cancel a pending request.

## 3. Approval and grants

Administrators see pending requests in **Requests** (with a badge in the menu) and can **approve, edit or reject** them. When
approving they can change the amount and the type (even turning a permanent request into a temporary one) and add a note.

Approval produces a **grant** (concession) that is **added** to the limit:

`effective limit = rule + active grants`

- A **temporary** grant lasts until the end of the period window **in which the approval happens** (not the one of the
  request: a request made on Sunday night and approved on Monday must not be born expired).
- A **permanent** grant applies to every following period ("+20 pages every month") until revoked.
- A group change moves the user at once; the new group's limits apply from then on.
- Approving twice (two administrators, a double click) grants once.
- Grants never touch history (`job_log` is not edited).

Administrators can also grant or revoke quota directly in **Requests**. Everything lands in the administrators' audit
([audit.md](audit.md)). The users list shows active grants as a coloured `+N`.

## 4. Reasons shown to users

When a job is refused, the IPP client receives the reason in English plus the portal address to ask for an exception.
The portal shows the same sentence translated ([languages.md](languages.md)). The audit stores the English reason.

## 5. Advanced permissions

Quotas say *how much*. Permissions say *what*, *where* and *when*. They are set on the **Advanced permissions** button at the
bottom of the group page (*Groups* → edit) and of the user page (*Users* → edit). The block is closed when everything is at the
default, and open when something differs.

![The edit page of a group: name, job and page limits, period, requestable, and the open Advanced permissions block with colour allowed and one-sided not allowed](../images/en/admin-group-edit.png)

| Permission | If it is not given |
|---|---|
| **Colour** | Prints come out in black and white. |
| **One-sided** | Prints come out double-sided (long edge), on the printers that have a duplex unit. |
| **Printers** | Only the printers in the list can be used. "All printers" is the default. |
| **Hours** | Printing works only on the chosen days between the two times. "Any time" is the default. |

Everything is allowed by default: a group with nothing set, and a user with nothing set, can do all of it.

Each permission is a row of buttons, one of which is chosen: *allowed* / *not allowed* for colour and one-sided, *all* / *only some* for
printers, *any time* / *some hours* for hours. The printers to tick appear only after *only some* is chosen, days and times only after
*some hours*. In a user's page every row starts with *as the group (…)*, which shows the group's value. On a phone the buttons stand one
under the other.

**Group and user.** The group sets the values. In a user's page each field is "as the group" (the default) or has a value of its
own, field by field: a user can have their own colour permission and still follow the group for printers and hours. The page
shows what the group says next to each field. Changing the group's permissions changes every user who follows it, and does not
send them a notice; changing a user's own permissions does (see "What the user sees"). An administrator is limited like anybody
else. The only exception is the **emergency administrator** (`SPOOLSTEWARD_BOOTSTRAP_ADMIN_USERNAME`, a local account): it has every
permission, its page shows the block switched off ("all permissions, can not be limited") and the server ignores anything posted
for it. It also has no group and no quota: it is not in the "administrator" group, has no job or page limit, and can not ask to change group. Quotas, limits and permissions of the "administrator" group apply to every other administrator. **Scanning is not limited** by any of this: a user can scan with a printer they may not print on.

### Printers

The list holds queue names, written in lower case (CUPS ignores the case). Three rules to remember:

- A printer added **after** the list was made is **not** allowed to someone with a list, until you tick it.
- If a queue is **renamed** ([printers.md](printers.md)), its name changes in every list by itself. If the list already had the new
  name, it has it once.
- If a queue is **removed**, its name stays in the lists (the page shows it as "not present"). A replacement printer given that
  name finds the permissions again.

### Hours

One window: the days of the week and two times (`HH:MM`), in the gateway's time zone (`SPOOLSTEWARD_TIMEZONE`, daylight saving
included). The end time is not included: `08:00` to `17:00` allows a job sent at 16:59, not at 17:00. If the end is earlier than the
start (`22:00` to `06:00`) the window goes past midnight and belongs to the day it starts: Friday 22:00 to Saturday 06:00 is
Friday's. Start and end can not be equal. The hours are checked **when the job is sent**: a job already accepted is not stopped when
the window closes.

### What happens to a job

The gateway checks, in this order: a blocked printer or maintenance mode (as always), then the permissions, then the quota.

| What the client asked | Not allowed | Result |
|---|---|---|
| A printer not in the list | | **Refused**, always |
| A time outside the hours | | **Refused**, always |
| Colour not stated (nothing, or `auto`) | no colour | Printed in black and white. Nobody is told |
| Colour stated (`color`) | no colour | `SPOOLSTEWARD_PERMISSION_MODE=adjust` (default): printed in black and white, the user gets a notice. `refuse`: refused |
| Sides not stated | no one-sided | Printed double-sided (long edge). Nobody is told |
| `one-sided` stated | no one-sided | `adjust`: printed double-sided, notice. `refuse`: refused |

The rules look at `print-color-mode`, `sides` and also at the options some drivers send by their PPD names (`ColorModel`,
`ColorMode`, `Duplex`), in the job and in the operation part of the request.

A printer that has no colour is never touched by the colour rule. A printer without a duplex unit (it lists only `one-sided`) is
never touched by the one-sided rule: the job prints one-sided and is not refused. The gateway asks the queue what it can do (one
question, remembered for a minute). If CUPS does not answer, nothing is forced blindly: only what the client stated explicitly is
corrected.

A refused job is written in the audit as `denied` with the reason, uses **no quota and no pages**, and the user gets a notice
(each refusal renews the open one and raises its counter, so a program that retries does not fill the bell). The IPP client receives the reason and the address
of the profile page, where the user sees their permissions and can ask for the missing one; it does not suggest asking for more quota.

### Read this before choosing the mode

1. **Windows and Android often send `sides=one-sided` or the colour in every job**, even if the person never touched the option.
   With `refuse`, many jobs of these users are refused until they change the option in the print dialog. That is why the default is
   `adjust`: the job is printed, corrected, and the user is told once.
2. **Black and white by force may not work** with some printers and drivers, above all with images that the phone has already
   turned into pixels in colour (iPhone / AirPrint). The gateway asks for black and white; the printer decides. If you need a
   guarantee, use `refuse`. Double-sided uses the standard attribute `sides`, which CUPS turns into the driver option by itself: it is more
   reliable, as long as the queue says it has the duplex unit.
3. **The announcement on the network (mDNS) is the same for everybody.** Printers a person may not use are still **visible** on
   their phone or computer, and a job sent to them is refused. The list of printers that the gateway gives after the login leaves them out.
4. **The hours are checked when the job is sent**, not while it prints.
5. **Scanning is not limited.**
6. **The emergency administrator is exempt** from the permissions and from the quotas: it belongs to no group.

### What the user sees

- *Profile*: the box "Your permissions" with colour, one-sided, printers and hours, and the link to ask for what is missing. The list of available printers and the ready-made
  addresses after creating a key show only the printers the person may use.
- *Print*: only the allowed printers; no colour choice without the colour permission; a double-sided choice (one-sided / long edge
  / short edge) on printers that have a duplex unit, only for those who may print one-sided; a banner with the allowed hours when
  they are outside them, and the job is refused.
- The bell: "refused", "quota reached" and "changed" notices when it happens (the "changed" one is *secondary*: it is read automatically after the time the user chose in `/me/settings`, 20 minutes by default), and one when an administrator changes the person's own permissions.

The administrators' log (*Actions*) records every change to a group's or a user's permissions with the old and the new values.

### Asking for a permission

A person who lacks a permission asks for it in the box "Your permissions" of their profile: the link **Ask** next to colour, one-sided
printing and hours, and **Ask for another printer** for the printers. The form offers only what the person lacks, and only printers
that exist now. A reason is required only if `SPOOLSTEWARD_REQUEST_REASON_REQUIRED=true` (500 characters at most). The request is refused when the person already has that permission,
or has already asked for the same thing and nobody has answered yet. The same brakes as the quota requests apply: at most
`SPOOLSTEWARD_MAX_OPEN_REQUESTS` open requests (of any kind together), and an unanswered request expires after `SPOOLSTEWARD_REQUEST_TTL_DAYS`.
The person can cancel it from their profile.

The administrator sees it in **Requests**, with a choice: **permanent**, or **until the end of** a date. The date counts through the
whole of that day in the portal's time zone (`SPOOLSTEWARD_TIMEZONE`) and must be today or later. Approving creates a *permission grant*
that **widens** what the group and the person's own rule give; it never takes anything away:

| Asked | The grant gives |
|---|---|
| Colour | colour is allowed |
| One-sided | one-sided printing is allowed |
| A printer | that printer is added to the person's list (if the person has no list, every printer is allowed already, so there is nothing to ask) |
| Outside the hours | the hours window does not apply |

Approving twice grants once, as for quota, and a refused approval (for example a date in the past) leaves the request waiting.
Active permission grants appear in the same *Active grants* list as the quota ones, with *Revoke*; ended and revoked ones go to the
history, and *Delete ended grants* removes both kinds. The administrators' log records each approval, direct grant, revocation and
deletion (the deletion with how many of each kind). The person's edit page lists their active permission grants.

An administrator can also grant a permission without a request: in *Grant without a request* choose **a permission**, what, and
permanent or until a date. Once a user is chosen, the form offers only what that user does not have already (a printer they can
already use is not in the list); when they have everything it says so and the button is off. The server refuses a permission the
user already has in any case. If JavaScript is off, the form shows all the fields and the server reads only those of the chosen kind.

The bell tells the person when the request is approved (with the end date) or rejected, when an administrator grants or revokes a
permission directly, and, within the hour after the end date, that a temporary permission has ended. An administrator is never told
about what they did to their own account. The emergency administrator has every permission and has nothing to ask for.
