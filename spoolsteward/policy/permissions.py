"""Advanced permissions: besides the quotas, a group or a user may be allowed or forbidden to print in colour, to print
one-sided (instead of being made double-sided), to use some printers only, and to print only at some hours.

  * `permission_rule` holds one row per group or user. On a group a missing value means "allowed / all printers / any time";
    on a user it means "as the group". A group with no row has everything. (`effective`)
  * `decide` is a pure function: from the permissions, the printer, what the printer can do and what the client asked, it
    says what happens to a job (allowed, refused, or printed with colour / sides corrected). The IPP gateway and the web
    print form both use it, so the two behave the same.
  * `permission_grant` adds to that for one user, for a while or for good: what an administrator approved after a request (or
    handed out directly). `effective` includes the active ones. (`spoolsteward.policy.grants` makes and revokes them.)
  * The emergency administrator is exempt (a wrong rule must never lock everybody out). It has no group and no quota either.
  * Scanning is not limited by any of this.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from spoolsteward import i18n
from spoolsteward.auth.users import is_emergency
from spoolsteward.db.models import PermissionGrant, PermissionRule, QuotaRequest, RequestKind, RequestStatus, SubjectType, User
from spoolsteward.i18n import N_, _
from spoolsteward.policy.ordering import key as queue_key

FIELDS = ("color", "one_sided", "printers_mode", "printers", "hours_mode", "hours_days", "hours_start", "hours_end")
MAX_PRINTERS = 200  # names in one "only these printers" list
NAME_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,127}$")  # what a CUPS queue name may be here (checked wherever a name comes from outside)
GRANTABLE = ("color", "one_sided", "printer", "hours")  # what a request or a grant can be about

# print-color-mode values that already mean black and white (or "let the printer decide"): not a request for colour
MONO_MODES = frozenset({"monochrome", "bi-level", "process-monochrome", "process-bi-level", "auto-monochrome", "auto"})
# PPD-style colour options (ColorModel=Gray ...) that mean black and white
GRAY_VALUES = frozenset({"gray", "grayscale", "mono", "monochrome", "black", "kgray"})
FORCE_MONO = ("print-color-mode", "monochrome")
FORCE_DUPLEX = ("sides", "two-sided-long-edge")
COLOR_PPD = ("ColorModel", "ColorMode")

DAYS = (N_("Mon"), N_("Tue"), N_("Wed"), N_("Thu"), N_("Fri"), N_("Sat"), N_("Sun"))  # date.weekday(): 0 = Monday


# ------------------------------------------------------------------------------------------------ hours

@dataclass(frozen=True)
class Window:
    """Hours of the day on some weekdays. `start` and `end` are minutes after midnight; the end minute is not included.
    start > end crosses midnight, and the whole window belongs to the day it starts on."""

    days: frozenset[int]
    start: int
    end: int


def fmt_minutes(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def parse_time(text: str) -> int:
    """'08:30' -> 510. ValueError when it is not a time of the day."""
    h, sep, m = (text or "").strip().partition(":")
    if not (sep and h.isascii() and m.isascii() and h.isdigit() and m.isdigit() and len(h) <= 2 and len(m) == 2):
        raise ValueError(text)
    h, m = int(h), int(m)
    if h > 23 or m > 59:
        raise ValueError(text)
    return h * 60 + m


def in_window(window: Window | None, now: datetime, tz: ZoneInfo) -> bool:
    """Is `now` inside the window (None = any time)? The local time of `tz` decides, daylight saving included."""
    if window is None:
        return True
    local = now.astimezone(tz)
    minute, day = local.hour * 60 + local.minute, local.weekday()
    if window.start < window.end:
        return day in window.days and window.start <= minute < window.end
    if window.start > window.end:  # crosses midnight: the part after midnight belongs to the day before
        return (day in window.days and minute >= window.start) or ((day - 1) % 7 in window.days and minute < window.end)
    return False  # start == end never saves (the form refuses it); if it ever is there, nobody prints rather than everybody


def describe_days(days) -> str:
    """{0,1,2,3,4} -> 'Mon–Fri', {0,2} -> 'Mon, Wed' (names in the language of the request)."""
    ordered, runs = sorted(set(days)), []
    for d in ordered:
        if runs and runs[-1][-1] == d - 1:
            runs[-1].append(d)
        else:
            runs.append([d])
    parts = [(f"{_(DAYS[r[0]])}–{_(DAYS[r[-1]])}" if len(r) > 2 else ", ".join(_(DAYS[d]) for d in r)) for r in runs]
    return ", ".join(parts)


def describe_window(window: Window) -> str:
    """'Mon–Fri 08:00–17:00'."""
    return f"{describe_days(window.days)} {fmt_minutes(window.start)}–{fmt_minutes(window.end)}"


def describe_hours(days: str, start: int, end: int) -> str:
    """The same from the three values stored in a notice."""
    return describe_window(Window(frozenset(int(c) for c in days or "" if c in "0123456"), int(start), int(end)))


# ------------------------------------------------------------------------------------------------ what a person has

@dataclass(frozen=True)
class Perms:
    color: bool = True
    one_sided: bool = True
    printers: frozenset[str] | None = None  # lower-case queue names; None = every printer
    window: Window | None = None  # None = any time
    exempt: bool = False  # the emergency administrator

    @property
    def restricted(self) -> bool:
        return not (self.exempt or (self.color and self.one_sided and self.printers is None and self.window is None))


def get_rule(session: Session, subject_type: SubjectType, subject_id: int | None) -> PermissionRule | None:
    if subject_id is None:
        return None
    return session.scalars(select(PermissionRule).where(
        PermissionRule.subject_type == subject_type, PermissionRule.subject_id == subject_id)).first()


def _clean_names(names) -> list[str]:
    return sorted({queue_key(n) for n in names or [] if isinstance(n, str) and n.strip()})


def set_rule(session: Session, subject_type: SubjectType, subject_id: int, **fields) -> PermissionRule | None:
    """Store the permissions of a group or user. Every field is replaced (None = default / as the group); a rule with
    nothing set is removed. The caller commits. The values are expected to be checked already (see the admin forms)."""
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown permission field {sorted(unknown)}")
    fields = {f: fields.get(f) for f in FIELDS}
    if fields["printers_mode"] != "list":
        fields["printers"] = None
    else:
        fields["printers"] = _clean_names(fields["printers"])[:MAX_PRINTERS]
    if fields["hours_mode"] != "window":
        fields["hours_days"] = fields["hours_start"] = fields["hours_end"] = None
    row = get_rule(session, subject_type, subject_id)
    if all(v is None for v in fields.values()):
        if row is not None:
            session.delete(row)
        return None
    if row is None:
        row = PermissionRule(subject_type=subject_type, subject_id=subject_id)
        session.add(row)
    for name, value in fields.items():
        setattr(row, name, value)
    row.updated_at = datetime.now(UTC)
    return row


def rule_fields(row: PermissionRule | None) -> dict:
    """The stored values as a plain dict (all None when there is no row): the form and the audit entry use it."""
    return {f: (getattr(row, f) if row is not None else None) for f in FIELDS}


def _window_of(row: PermissionRule) -> Window | None:
    days = frozenset(int(c) for c in row.hours_days or "" if c in "0123456")
    if row.hours_start is None or row.hours_end is None:
        return None
    return Window(days, row.hours_start, row.hours_end)


def _layer(perms: Perms, row: PermissionRule | None) -> Perms:
    """`row` on top of `perms`: only the fields the row sets replace what is below."""
    if row is None:
        return perms
    color = perms.color if row.color is None else bool(row.color)
    one_sided = perms.one_sided if row.one_sided is None else bool(row.one_sided)
    printers, window = perms.printers, perms.window
    if row.printers_mode == "all":
        printers = None
    elif row.printers_mode == "list":
        printers = frozenset(_clean_names(row.printers))
    if row.hours_mode == "any":
        window = None
    elif row.hours_mode == "window":
        window = _window_of(row) or window
    return Perms(color, one_sided, printers, window)


def group_perms(session: Session, group_id: int | None) -> Perms:
    """What the group alone gives (what a user with nothing of their own gets)."""
    return _layer(Perms(), get_rule(session, SubjectType.group, group_id))


def effective(session: Session, user: User, now: datetime | None = None) -> Perms:
    """The group's rule, then the user's own rule on top of it field by field. The emergency administrator has everything."""
    if is_emergency(user):
        return Perms(exempt=True)
    perms = _layer(group_perms(session, user.group_id), get_rule(session, SubjectType.user, user.id))
    return _with_grants(session, user, perms, now or datetime.now(UTC))


def load(sm, user: User, now: datetime | None = None) -> Perms:
    """`effective` with its own short session (blocking: run it in a worker thread from async code)."""
    with sm() as session:
        return effective(session, user, now)


def _active_grant_filter(user: User, now: datetime):
    return (PermissionGrant.user_id == user.id, PermissionGrant.revoked_at.is_(None), PermissionGrant.valid_from <= now,
            (PermissionGrant.valid_until.is_(None)) | (PermissionGrant.valid_until > now))


def active_grants(session: Session, user: User, now: datetime) -> list[PermissionGrant]:
    """The grants that count right now (not revoked, started, not ended), oldest first."""
    return list(session.scalars(select(PermissionGrant).where(*_active_grant_filter(user, now)).order_by(PermissionGrant.id)))


def _with_grants(session: Session, user: User, perms: Perms, now: datetime) -> Perms:
    """`perms` widened by the grants of this user that count now. A grant only ever adds: colour, one-sided, any hours, or one
    more printer (when a list applies; with no list every printer is allowed already). One query."""
    rows = session.execute(select(PermissionGrant.perm, PermissionGrant.printer).where(*_active_grant_filter(user, now))).all()
    if not rows:
        return perms
    color, one_sided, printers, window = perms.color, perms.one_sided, perms.printers, perms.window
    for perm, printer in rows:
        if perm == "color":
            color = True
        elif perm == "one_sided":
            one_sided = True
        elif perm == "hours":
            window = None
        elif perm == "printer" and printer and printers is not None:
            printers = printers | {queue_key(printer)}
    return Perms(color, one_sided, printers, window, perms.exempt)


def has_permission(perms: Perms, perm: str, printer: str | None = None) -> bool:
    """Does this person already have what a request would be about?"""
    if perms.exempt:
        return True
    return {"color": perms.color, "one_sided": perms.one_sided, "hours": perms.window is None,
            "printer": printer_allowed(perms, printer)}.get(perm, False)


def describe_grant(perm: str, printer: str | None = None) -> str:
    """'colour printing', 'printer hp', ... in the language of the request."""
    if perm == "printer":
        return _("printer %(printer)s", printer=printer or "?")
    return {"color": _("colour printing"), "one_sided": _("one-sided printing"), "hours": _("printing outside the hours")}.get(perm, perm)


def notify_expired(sm, now: datetime | None = None) -> int:
    """Tell each person, once, that a permission they were given for a while has ended (their bell). Revoked grants are not
    announced (they were told when it happened). Returns how many notices were sent."""
    from spoolsteward import notices

    now = now or datetime.now(UTC)
    sent = 0
    with sm() as db:
        for g in list(db.scalars(select(PermissionGrant).where(
                PermissionGrant.valid_until.is_not(None), PermissionGrant.valid_until <= now,
                PermissionGrant.expiry_notified_at.is_(None), PermissionGrant.revoked_at.is_(None)))):
            notices.notify_user(sm, g.user_id, "permission_expired", {"perm": g.perm, "printer": g.printer}, key=f"{g.perm}:{g.printer or ''}")
            g.expiry_notified_at = now
            sent += 1
        db.commit()
    return sent


def allowed_queues(perms: Perms, queues: list, name: Callable = lambda q: q["name"]) -> list:
    """The printers of `queues` this person may print on (all of them when no list applies)."""
    if perms.printers is None:
        return list(queues)
    return [q for q in queues if queue_key(name(q)) in perms.printers]


def printer_allowed(perms: Perms, printer: str | None) -> bool:
    return perms.printers is None or (printer is not None and queue_key(printer) in perms.printers)


def rename_printer(session: Session, old: str, new: str) -> int:
    """A queue was renamed: its name follows in every "only these printers" list (a list that already had the new name
    just keeps one). Returns how many lists changed. The caller commits."""
    o, n, changed = queue_key(old), queue_key(new), 0
    for row in session.scalars(select(PermissionRule).where(PermissionRule.printers.is_not(None))):
        names = _clean_names(row.printers)
        if o in names:
            row.printers = sorted({n if x == o else x for x in names})
            changed += 1
    # a printer given for a while, or asked for and not answered yet, follows too
    for g in session.scalars(select(PermissionGrant).where(PermissionGrant.perm == "printer", PermissionGrant.printer == o)):
        g.printer = n
    for r in session.scalars(select(QuotaRequest).where(QuotaRequest.kind == RequestKind.permission,
                                                        QuotaRequest.status == RequestStatus.pending)):
        p = dict(r.params_json or {})
        if p.get("perm") == "printer" and p.get("printer") == o:
            p["printer"] = n
            r.params_json = p  # a new dict: the JSON column notices a reassignment, not a change inside
    return changed


# ------------------------------------------------------------------------------------------------ one job

@dataclass
class PermDecision:
    """`reason` is English (audit, IPP status message); `text()` says the same in the language of the request."""

    allowed: bool = True
    why: str | None = None  # printer | hours | color | one_sided (what refused it)
    reason: str | None = None
    localized: Callable[[], str] | None = field(default=None, compare=False, repr=False)
    set_attrs: dict = field(default_factory=dict)  # IPP job attributes to set: {"print-color-mode": "monochrome"}
    drop_attrs: set = field(default_factory=set)  # attributes to remove from the request (PPD-style options)
    changed: list = field(default_factory=list)  # ["color", "one_sided"]: what was corrected after being asked for
    detail: dict = field(default_factory=dict)  # extra facts for the user's notice (the allowed hours)

    def text(self) -> str | None:
        if self.localized is not None:
            return self.localized()
        return _(self.reason) if self.reason else self.reason


def _refusal(why: str, reason: str, localized: Callable[[], str], **detail) -> PermDecision:
    return PermDecision(False, why, reason, localized, detail=detail)


def _english_window(window: Window) -> str:
    with i18n.using("en"):
        return describe_window(window)


def explicit_color(attrs: dict) -> bool:
    """Did the client ask for colour? Absent, `auto` and the black and white modes are not a request for it."""
    mode = attrs.get("print-color-mode")
    if isinstance(mode, str) and mode.strip().lower() not in MONO_MODES:
        return True
    for name in COLOR_PPD:
        value = attrs.get(name)
        if isinstance(value, str) and value.strip().lower() not in GRAY_VALUES:
            return True
    return False


def explicit_one_sided(attrs: dict) -> bool:
    sides, duplex = attrs.get("sides"), attrs.get("Duplex")
    return (isinstance(sides, str) and sides.strip().lower() == "one-sided") or \
           (isinstance(duplex, str) and duplex.strip().lower() == "none")


def decide(perms: Perms, printer: str | None, caps: dict, job_attrs: dict, mode: str, now: datetime, tz: ZoneInfo) -> PermDecision:
    """What happens to one print job. Order: printer, hours, colour, sides.

    `caps` = {"color": bool|None, "duplex": bool|None} of the printer (None = CUPS did not say: then only what the client
    asked for explicitly is acted upon, nothing is forced blindly). `job_attrs` = the values of print-color-mode, sides,
    ColorModel, ColorMode and Duplex found in the request. `mode`: "adjust" prints a job that asks for colour / one-sided
    without the right in black and white / double-sided and reports it; "refuse" refuses it. Printers and hours always refuse.
    A client that did not ask (attribute missing, or `auto`) is corrected silently in both modes."""
    if perms.exempt:
        return PermDecision()
    if not printer_allowed(perms, printer):
        name = printer or "?"
        return _refusal("printer", f"printing on {name} is not allowed for this account",
                        lambda: _("printing on %(printer)s is not allowed for this account", printer=name))
    if perms.window is not None and not in_window(perms.window, now, tz):
        window = perms.window
        return _refusal("hours", f"printing is allowed only {_english_window(window)}",
                        lambda: _("printing is allowed only %(window)s", window=describe_window(window)),
                        days="".join(str(d) for d in sorted(window.days)), start=window.start, end=window.end)
    out = PermDecision()
    if not perms.color and caps.get("color") is not False:
        asked = explicit_color(job_attrs)
        if asked and mode == "refuse":
            return _refusal("color", "colour printing is not allowed for this account",
                            lambda: _("colour printing is not allowed for this account"))
        mode_now = str(job_attrs.get("print-color-mode", "")).strip().lower()
        already = not asked and mode_now in MONO_MODES - {"auto"}  # the client already asked for black and white
        if not already and (asked or caps.get("color") is True):
            out.set_attrs[FORCE_MONO[0]] = FORCE_MONO[1]
            out.drop_attrs |= {n for n in COLOR_PPD if n in job_attrs}
            if asked:
                out.changed.append("color")
    if not perms.one_sided and caps.get("duplex") is not False:
        asked = explicit_one_sided(job_attrs)
        if asked and mode == "refuse":
            return _refusal("one_sided", "one-sided printing is not allowed for this account",
                            lambda: _("one-sided printing is not allowed for this account"))
        sides_now, duplex_now = str(job_attrs.get("sides", "")).strip().lower(), str(job_attrs.get("Duplex", "None")).strip().lower()
        already = not asked and (sides_now.startswith("two-sided") or duplex_now != "none")  # the client already asked for double-sided
        if not already and (asked or caps.get("duplex") is True):
            out.set_attrs[FORCE_DUPLEX[0]] = FORCE_DUPLEX[1]
            out.drop_attrs |= {n for n in ("Duplex",) if n in job_attrs}
            if asked:
                out.changed.append("one_sided")
    return out


def notify_refused(sm, user_id: int, printer: str | None, d: PermDecision) -> None:
    """Tell the person in their bell that a print was refused. Every refusal counts (the notice is renewed and the bell
    lights up again), but a program that retries does not fill the list."""
    from spoolsteward import notices

    notices.notify_user_counted(sm, user_id, "job_refused", key=f"{d.why}:{printer}",
                             detail={"printer": printer, "why": d.why, **d.detail})


def notify_adjusted(sm, user_id: int, printer: str | None, d: PermDecision) -> None:
    """Tell the person that a print was made in black and white / double-sided because they asked for something they may not have."""
    from spoolsteward import notices

    notices.notify_user_counted(sm, user_id, "job_adjusted", key=f"adjusted:{printer}",
                                detail={"printer": printer, "changed": list(d.changed)})


def notify_quota(sm, user_id: int, printer: str | None, quota: dict) -> None:
    """Tell the person that a print was refused because a quota limit is reached (same renewal rule as a refusal)."""
    from spoolsteward import notices

    notices.notify_user_counted(sm, user_id, "quota_reached", key=f"{quota.get('period')}:{quota.get('metric')}",
                                detail={"printer": printer, **quota})
