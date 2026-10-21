"""Advanced permissions: colour, one-sided prints, printers and hours, for groups and users. The decision itself (a pure
function), the rules that give it its input, the IPP gateway and the web print form that apply it, the admin pages that set
it, and the names of past queues offered when a queue is renamed."""

import base64
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from spoolsteward import notices
from spoolsteward.auth import keys
from spoolsteward.config import settings
from spoolsteward.db.models import (
    AdminAudit, Group, JobLog, Notice, PermissionRule, PrinterFormer, SubjectType, User,
)
from spoolsteward.ipp import codec as c
from spoolsteward.policy import audit, discovery, permissions
from spoolsteward.policy.permissions import Perms, Window, decide, in_window
from tests.conftest import csrf_of_page, make_user, portal_login

GOOD = "correct horse battery"
ZUTC = ZoneInfo("UTC")
ROME = ZoneInfo("Europe/Rome")
WED_10 = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)   # a Wednesday
NO_COLOR = Perms(color=False)
NO_ONE_SIDED = Perms(one_sided=False)
BOTH = {"color": True, "duplex": True}


def dec(perms, attrs=None, caps=BOTH, mode="adjust", printer="hp", now=WED_10, tz=ZUTC):
    return decide(perms, printer, caps, attrs or {}, mode, now, tz)


# ------------------------------------------------------------------------------------------------ the decision

@pytest.mark.parametrize("attrs", [{}, {"print-color-mode": "auto"}, {"ColorModel": "Gray"}, {"ColorMode": "Monochrome"}])
@pytest.mark.parametrize("mode", ["adjust", "refuse"])
def test_colour_not_asked_is_made_black_and_white_silently_in_both_modes(attrs, mode):
    d = dec(NO_COLOR, attrs, mode=mode)
    assert d.allowed and d.set_attrs == {"print-color-mode": "monochrome"} and d.changed == []   # nobody is told


@pytest.mark.parametrize("mode_value", ["monochrome", "bi-level", "process-monochrome"])
def test_a_client_that_already_asks_for_black_and_white_is_left_as_it_is(mode_value):
    d = dec(NO_COLOR, {"print-color-mode": mode_value}, mode="refuse")
    assert d.allowed and d.set_attrs == {} and d.changed == []


@pytest.mark.parametrize("attrs", [{"print-color-mode": "color"}, {"ColorModel": "RGB"}, {"ColorMode": "Color"},
                                   {"print-color-mode": "auto", "ColorModel": "CMYK"}])
def test_colour_asked_against_the_permission_adjust_prints_in_black_and_white_and_says_so(attrs):
    d = dec(NO_COLOR, attrs, mode="adjust")
    assert d.allowed and d.changed == ["color"] and d.set_attrs == {"print-color-mode": "monochrome"}
    assert d.drop_attrs == {n for n in ("ColorModel", "ColorMode") if n in attrs}


def test_colour_asked_against_the_permission_refuse_refuses_with_the_reason():
    d = dec(NO_COLOR, {"print-color-mode": "color"}, mode="refuse")
    assert not d.allowed and d.why == "color" and "colour" in d.reason and d.set_attrs == {}


def test_a_printer_without_colour_is_not_touched_and_unknown_capabilities_force_nothing_blindly():
    assert dec(NO_COLOR, {"print-color-mode": "color"}, caps={"color": False, "duplex": True}, mode="refuse").set_attrs == {}
    unknown = {"color": None, "duplex": None}
    d = dec(NO_COLOR, {}, caps=unknown)                                        # nothing asked, nothing known: left alone
    assert d.allowed and d.set_attrs == {} and d.changed == []
    d = dec(NO_COLOR, {"print-color-mode": "color"}, caps=unknown)             # asked: acted upon even if CUPS did not answer
    assert d.changed == ["color"]
    assert not dec(NO_COLOR, {"print-color-mode": "color"}, caps=unknown, mode="refuse").allowed


@pytest.mark.parametrize("attrs", [{"sides": "one-sided"}, {"Duplex": "None"}, {"Duplex": "none"}])
def test_one_sided_asked_against_the_permission(attrs):
    d = dec(NO_ONE_SIDED, attrs)
    assert d.allowed and d.changed == ["one_sided"] and d.set_attrs == {"sides": "two-sided-long-edge"}
    assert d.drop_attrs == ({"Duplex"} if "Duplex" in attrs else set())
    r = dec(NO_ONE_SIDED, attrs, mode="refuse")
    assert not r.allowed and r.why == "one_sided"


def test_sides_not_asked_are_made_double_sided_silently_and_a_double_sided_request_is_kept():
    d = dec(NO_ONE_SIDED, {}, mode="refuse")
    assert d.allowed and d.changed == [] and d.set_attrs == {"sides": "two-sided-long-edge"}
    assert dec(NO_ONE_SIDED, {"sides": "auto"}).set_attrs == {"sides": "two-sided-long-edge"}
    for kept in ({"sides": "two-sided-short-edge"}, {"sides": "two-sided-long-edge"}, {"Duplex": "DuplexNoTumble"}):
        d = dec(NO_ONE_SIDED, kept, mode="refuse")
        assert d.allowed and d.set_attrs == {} and d.changed == [], kept                  # the short edge the client chose stays


def test_a_printer_without_a_duplex_unit_prints_one_sided_without_refusal():
    d = dec(NO_ONE_SIDED, {"sides": "one-sided"}, caps={"color": True, "duplex": False}, mode="refuse")
    assert d.allowed and d.set_attrs == {} and d.changed == []
    unknown = dec(NO_ONE_SIDED, {}, caps={"color": None, "duplex": None})
    assert unknown.allowed and unknown.set_attrs == {}


def test_order_is_printer_then_hours_then_colour_then_sides():
    both = Perms(color=False, one_sided=False, printers=frozenset({"other"}), window=Window(frozenset({5}), 0, 60))
    assert dec(both, {"print-color-mode": "color"}, mode="refuse").why == "printer"
    both = Perms(color=False, one_sided=False, window=Window(frozenset({5}), 0, 60))
    assert dec(both, {"print-color-mode": "color"}, mode="refuse").why == "hours"
    both = Perms(color=False, one_sided=False)
    assert dec(both, {"print-color-mode": "color", "sides": "one-sided"}, mode="refuse").why == "color"
    d = dec(both, {"print-color-mode": "color", "sides": "one-sided"})
    assert d.changed == ["color", "one_sided"]


def test_printers_and_hours_refuse_in_adjust_mode_too_and_the_emergency_account_is_never_limited():
    assert not dec(Perms(printers=frozenset({"x"})), mode="adjust").allowed
    assert not dec(Perms(printers=frozenset({"x"})), printer=None).allowed
    assert dec(Perms(printers=frozenset({"hp"})), printer="HP").allowed                       # CUPS ignores case
    assert dec(Perms(exempt=True, color=False, printers=frozenset())).set_attrs == {}
    d = dec(Perms(window=Window(frozenset({0}), 480, 1020)), mode="adjust")
    assert not d.allowed and d.why == "hours" and d.detail == {"days": "0", "start": 480, "end": 1020}
    assert "Mon 08:00–17:00" in d.reason


# ------------------------------------------------------------------------------------------------ hours

def local(y, mo, d, h, mi=0, tz=ROME):
    return datetime(y, mo, d, h, mi, tzinfo=tz).astimezone(UTC)


def test_a_normal_window_on_some_weekdays_end_excluded():
    w = Window(frozenset({0, 1, 2, 3, 4}), 8 * 60, 17 * 60)
    assert in_window(w, local(2026, 10, 14, 8, 0), ROME) and in_window(w, local(2026, 10, 14, 16, 59), ROME)
    assert not in_window(w, local(2026, 10, 14, 7, 59), ROME) and not in_window(w, local(2026, 10, 14, 17, 0), ROME)
    assert not in_window(w, local(2026, 10, 17, 10, 0), ROME)                         # a Saturday
    assert in_window(None, local(2026, 10, 17, 3, 0), ROME)


def test_a_window_across_midnight_belongs_to_the_day_it_starts():
    w = Window(frozenset({4}), 22 * 60, 6 * 60)                                       # Friday 22:00 -> Saturday 06:00
    assert in_window(w, local(2026, 10, 16, 23, 0), ROME)                             # Friday night
    assert in_window(w, local(2026, 10, 17, 5, 59), ROME)                             # still Friday's window, on Saturday
    assert not in_window(w, local(2026, 10, 17, 6, 0), ROME) and not in_window(w, local(2026, 10, 17, 23, 0), ROME)
    assert not in_window(w, local(2026, 10, 16, 5, 0), ROME)                          # Friday 05:00 belongs to Thursday's
    sunday = Window(frozenset({6}), 22 * 60, 6 * 60)                                  # Sunday night runs into Monday
    assert in_window(sunday, local(2026, 10, 19, 1, 0), ROME)


def test_the_window_follows_the_clock_of_the_time_zone_across_daylight_saving():
    w = Window(frozenset(range(7)), 2 * 60 + 30, 3 * 60 + 30)
    # 2026-03-29: clocks go 02:00 -> 03:00 in Rome. 01:30 UTC is 03:30 local (outside), 01:00 UTC is 03:00 local (inside)
    assert in_window(w, datetime(2026, 3, 29, 1, 0, tzinfo=UTC), ROME)
    assert not in_window(w, datetime(2026, 3, 29, 1, 30, tzinfo=UTC), ROME)
    # 2026-10-25: clocks go 03:00 -> 02:00; 02:30 happens twice (00:30 UTC and 01:30 UTC), both inside
    assert in_window(w, datetime(2026, 10, 25, 0, 30, tzinfo=UTC), ROME)
    assert in_window(w, datetime(2026, 10, 25, 1, 30, tzinfo=UTC), ROME)
    assert not in_window(w, datetime(2026, 10, 25, 2, 30, tzinfo=UTC), ROME)


def test_start_equal_to_end_lets_nobody_in_and_the_text_is_readable():
    assert not in_window(Window(frozenset(range(7)), 600, 600), WED_10, ZUTC)
    assert permissions.describe_window(Window(frozenset({0, 1, 2, 3, 4}), 480, 1020)) == "Mon–Fri 08:00–17:00"
    assert permissions.describe_days({0, 2, 5, 6}) == "Mon, Wed, Sat, Sun"
    assert permissions.describe_days({1, 2}) == "Tue, Wed"
    assert permissions.parse_time("08:05") == 485 and permissions.parse_time("23:59") == 1439
    for bad in ("", "8", "24:00", "12:60", "ab:cd", "12:5", "1:2:3"):
        with pytest.raises(ValueError):
            permissions.parse_time(bad)


# ------------------------------------------------------------------------------------------------ who has what

def gid_of(sm, name):
    with sm() as s:
        return s.scalars(select(Group).where(Group.name == name)).one().id


def uid_of(sm, name):
    with sm() as s:
        return s.scalars(select(User).where(User.username == name)).one().id


def perms_of(sm, name):
    with sm() as s:
        return permissions.effective(s, s.scalars(select(User).where(User.username == name)).one(), WED_10)


def test_a_group_without_a_rule_has_everything_and_the_user_inherits_field_by_field(portal):
    make_user(portal.sm, "ann", group="staff")
    assert perms_of(portal.sm, "ann") == Perms()
    with portal.sm() as s:
        permissions.set_rule(s, SubjectType.group, gid_of(portal.sm, "staff"), color=False, printers_mode="list", printers=["HP", "Brother"],
                             hours_mode="window", hours_days="01234", hours_start=480, hours_end=1020)
        s.commit()
    p = perms_of(portal.sm, "ann")
    assert not p.color and p.one_sided and p.printers == frozenset({"hp", "brother"}) and p.window == Window(frozenset(range(5)), 480, 1020)
    with portal.sm() as s:   # the user overrides ONE field and leaves the others "as the group"
        permissions.set_rule(s, SubjectType.user, uid_of(portal.sm, "ann"), color=True, printers_mode="all")
        s.commit()
    p = perms_of(portal.sm, "ann")
    assert p.color and p.printers is None and p.window is not None
    with portal.sm() as s:
        permissions.set_rule(s, SubjectType.user, uid_of(portal.sm, "ann"), hours_mode="any")
        s.commit()
        assert permissions.get_rule(s, SubjectType.user, uid_of(portal.sm, "ann")).hours_start is None   # replaced, not merged
    p = perms_of(portal.sm, "ann")                               # every field was replaced: the others are the group's again
    assert p.window is None and not p.color and p.printers == frozenset({"hp", "brother"})


def test_a_rule_with_nothing_set_is_removed_and_unknown_fields_are_refused(portal):
    make_user(portal.sm, "ann")
    with portal.sm() as s:
        permissions.set_rule(s, SubjectType.user, uid_of(portal.sm, "ann"), one_sided=False)
        s.commit()
        assert s.query(PermissionRule).count() == 1
        permissions.set_rule(s, SubjectType.user, uid_of(portal.sm, "ann"))
        s.commit()
        assert s.query(PermissionRule).count() == 0
        with pytest.raises(ValueError):
            permissions.set_rule(s, SubjectType.user, 1, root=True)


def test_the_emergency_administrator_is_exempt_but_another_administrator_is_limited(portal):
    make_user(portal.sm, settings.bootstrap_admin_username, admin=True, group="administrator")
    make_user(portal.sm, "boss", admin=True, group="administrator")
    with portal.sm() as s:
        permissions.set_rule(s, SubjectType.group, gid_of(portal.sm, "administrator"), color=False)
        s.commit()
    assert perms_of(portal.sm, settings.bootstrap_admin_username) == Perms(exempt=True)
    assert not perms_of(portal.sm, "boss").color
    assert not perms_of(portal.sm, "boss").exempt
    # a RADIUS account that has the same name is not the emergency account
    make_user(portal.sm, "imposter", provider="radius")
    with portal.sm() as s:
        assert permissions.effective(s, User(username=settings.bootstrap_admin_username, provider="radius", id=999)).exempt is False


def test_rename_follows_in_every_list_and_joins_a_list_that_already_had_the_new_name(portal):
    with portal.sm() as s:
        permissions.set_rule(s, SubjectType.group, 1, printers_mode="list", printers=["Old", "Other"])
        permissions.set_rule(s, SubjectType.group, 2, printers_mode="list", printers=["old", "new"])
        permissions.set_rule(s, SubjectType.group, 3, printers_mode="list", printers=["Other"])
        permissions.set_rule(s, SubjectType.user, 1, color=False)                       # no list at all
        s.commit()
        assert permissions.rename_printer(s, "OLD", "New") == 2
        s.commit()
        got = {r.subject_id: r.printers for r in s.scalars(select(PermissionRule)) if r.printers}
        assert got == {1: ["new", "other"], 2: ["new"], 3: ["other"]}


# ------------------------------------------------------------------------------------------------ the IPP gateway

def basic(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode(), "Content-Type": "application/ipp"}


def kw(name, value):
    return c.Attr(name, [c.Val(c.TAG_KEYWORD, value)])


class FakeCups:
    """Stands in for CUPS: remembers what reached it and answers what the gateway asks."""

    def __init__(self):
        self.seen: list[tuple[str, c.IppMessage]] = []
        self.queues = {"color": ("ipp://p1/ipp/print", True, ["one-sided", "two-sided-long-edge"]),
                       "bw": ("ipp://p2/ipp/print", False, ["one-sided"]),
                       "pdf": ("cups-pdf:/", True, ["one-sided", "two-sided-long-edge"])}
        self.caps_asked = 0

    def _printer(self, name):
        uri, color, sides = self.queues[name]
        return c.Group(c.TAG_PRINTER, [
            c.Attr("printer-name", [c.Val(c.TAG_NAME, name)]), c.Attr("printer-info", [c.Val(c.TAG_TEXT, name)]),
            c.Attr("device-uri", [c.Val(c.TAG_URI, uri)]), c.Attr("printer-is-accepting-jobs", [c.Val(c.TAG_BOOLEAN, True)]),
            c.Attr("printer-state", [c.Val(c.TAG_ENUM, 3)]), c.Attr("queued-job-count", [c.Val(c.TAG_INTEGER, 0)]),
            c.Attr("color-supported", [c.Val(c.TAG_BOOLEAN, color)]),
            c.Attr("sides-supported", [c.Val(c.TAG_KEYWORD, s) for s in sides])])

    def handler(self, req: httpx.Request) -> httpx.Response:
        msg = c.decode(req.content)
        self.seen.append((req.url.path, msg))
        ok = c.make_error_response(0, msg.request_id)
        if msg.code == c.GET_PRINTER_ATTRIBUTES:
            self.caps_asked += 1
            name = req.url.path.rsplit("/", 1)[-1]
            if name not in self.queues:
                return self._reply(c.make_error_response(0x0406, msg.request_id))
            ok.groups.append(self._printer(name))
        elif msg.code == 0x4002:
            ok.groups += [self._printer(n) for n in self.queues]
        elif msg.code == 0x4003:      # CUPS-Add-Modify-Printer
            uri = msg.get(c.TAG_OPERATION, "printer-uri")
            self.queues[uri.rsplit("/", 1)[-1]] = (msg.get(c.TAG_PRINTER, "device-uri"), True, ["one-sided", "two-sided-long-edge"])
        elif msg.code == 0x4004:      # CUPS-Delete-Printer
            self.queues.pop(msg.get(c.TAG_OPERATION, "printer-uri").rsplit("/", 1)[-1], None)
        elif msg.code in (c.PRINT_JOB, c.CREATE_JOB):
            ok.groups.append(c.Group(c.TAG_JOB, [c.Attr("job-id", [c.Val(c.TAG_INTEGER, 7)])]))
        return self._reply(ok)

    @staticmethod
    def _reply(msg):
        return httpx.Response(200, content=c.encode(msg), headers={"content-type": "application/ipp"})

    def jobs(self):
        return [m for p, m in self.seen if m.code in (c.PRINT_JOB, c.CREATE_JOB)]

    def job_attr(self, msg, name):
        """The value of `name` wherever the gateway left it (job or operation group), or None."""
        for g in msg.groups:
            if g.tag in (c.TAG_JOB, c.TAG_OPERATION) and g.get(name):
                return g.get(name).first
        return None


@pytest.fixture
async def gw(portal, monkeypatch):
    """The gateway over https in front of a fake CUPS, with a user "ann" (group "staff") and a print key for her."""
    monkeypatch.setattr(settings, "ipp_auth", "keys")
    monkeypatch.setattr(settings, "permission_mode", "adjust")
    cups = FakeCups()
    portal.app.state.upstream = httpx.AsyncClient(base_url="http://cups", transport=httpx.MockTransport(cups.handler))
    portal.app.state.queue_caps_cache = None
    portal.app.state.queues_cache = None
    make_user(portal.sm, "ann", password=GOOD, group="staff")
    with portal.sm() as s:
        _, portal.key = keys.create_key(s, s.scalars(select(User).where(User.username == "ann")).one(), "k", 30)
    portal.cups = cups
    yield portal
    del portal.app.state.upstream


def limit(cl, group="staff", **fields):
    with cl.sm() as s:
        permissions.set_rule(s, SubjectType.group, gid_of(cl.sm, group), **fields)
        s.commit()
    cl.app.state.queue_caps_cache = None


def job(op=c.PRINT_JOB, printer="color", job_attrs=(), op_attrs=()):
    r = c.make_request(op, 1, printer_uri=f"ipp://gw.test/printers/{printer}", job_name="doc.txt", document_format="text/plain")
    r.group(c.TAG_OPERATION).attrs.extend(op_attrs)
    if job_attrs:
        r.groups.append(c.Group(c.TAG_JOB, list(job_attrs)))
    r.data = b"hello\n"
    return r


async def send(cl, req, printer="color", user="ann", secret=None):
    r = await cl.post(f"/printers/{printer}", content=c.encode(req), headers=basic(user, secret or cl.key))
    assert r.status_code == 200, r.text
    return c.decode(r.content)


def log_rows(cl):
    with cl.sm() as s:
        return [(j.state.value, j.printer, j.denied_reason, j.options_json) for j in s.scalars(select(JobLog).order_by(JobLog.id))]


def open_notices(cl, kind):
    with cl.sm() as s:
        return [n for n in s.scalars(select(Notice).where(Notice.kind == kind)) if n.dismissed_at is None]


async def test_a_forbidden_printer_is_refused_audited_without_quota_and_told_once(gw):
    limit(gw, printers_mode="list", printers=["bw"])
    for _ in range(2):
        out = await send(gw, job(printer="color"), "color")
        assert out.code == c.STATUS_NOT_POSSIBLE
        assert "not allowed for this account" in out.get(c.TAG_OPERATION, "status-message")
        assert "/me" in out.get(c.TAG_OPERATION, "status-message") and "more quota" not in out.get(c.TAG_OPERATION, "status-message")
    assert gw.cups.jobs() == []                                                         # never reached CUPS
    assert [(r[0], r[1]) for r in log_rows(gw)] == [("denied", "color"), ("denied", "color")]
    assert "printing on color is not allowed" in log_rows(gw)[0][2]
    [n] = open_notices(gw, "job_refused")                                               # two attempts, ONE notice
    assert n.detail_json == {"printer": "color", "why": "printer"} and n.key == "printer:color"
    assert (await send(gw, job(printer="bw"), "bw")).code == 0                          # the allowed one still works
    gw.cups.seen.clear()


async def test_outside_the_hours_the_job_is_refused_and_inside_it_goes_through(gw, monkeypatch):
    limit(gw, hours_mode="window", hours_days="01234", hours_start=480, hours_end=1020)
    monkeypatch.setattr(audit, "now_utc", lambda: datetime(2026, 10, 17, 10, 0, tzinfo=UTC))   # a Saturday
    out = await send(gw, job())
    assert out.code == c.STATUS_NOT_POSSIBLE and "Mon–Fri 08:00–17:00" in out.get(c.TAG_OPERATION, "status-message")
    [row] = log_rows(gw)
    assert row[0] == "denied" and "Mon–Fri" in row[2]
    [n] = open_notices(gw, "job_refused")
    assert n.detail_json["why"] == "hours" and (n.detail_json["days"], n.detail_json["start"], n.detail_json["end"]) == ("01234", 480, 1020)
    monkeypatch.setattr(audit, "now_utc", lambda: datetime(2026, 10, 14, 10, 0, tzinfo=UTC))   # a Wednesday
    assert (await send(gw, job())).code == 0 and len(gw.cups.jobs()) == 1


async def test_adjust_forwards_black_and_white_and_double_sided_and_tells_the_user(gw):
    limit(gw, color=False, one_sided=False)
    out = await send(gw, job(job_attrs=[kw("print-color-mode", "color"), kw("sides", "one-sided"), kw("ColorModel", "RGB"), kw("Duplex", "None"),
                                        kw("media", "iso_a4_210x297mm")]))
    assert out.code == 0
    [sent] = gw.cups.jobs()
    assert gw.cups.job_attr(sent, "print-color-mode") == "monochrome" and gw.cups.job_attr(sent, "sides") == "two-sided-long-edge"
    names = {a.name for g in sent.groups for a in g.attrs}
    assert "ColorModel" not in names and "Duplex" not in names and "media" in names     # what was not touched stays
    [(state, _, _, options)] = log_rows(gw)
    assert state == "accepted" and options["adjusted"] == ["color", "one_sided"] and options["color"] == "color" and options["sides"] == "one-sided"
    [adj] = open_notices(gw, "job_adjusted")
    assert adj.detail_json == {"printer": "color", "changed": ["color", "one_sided"]}
    await send(gw, job(job_attrs=[kw("print-color-mode", "color")]))
    assert len(open_notices(gw, "job_adjusted")) == 1                                   # not again while that one is open


async def test_options_put_in_the_operation_group_are_seen_and_replaced(gw):
    limit(gw, color=False)
    await send(gw, job(op_attrs=[kw("print-color-mode", "color")]))                     # no job group at all
    [sent] = gw.cups.jobs()
    assert sent.group(c.TAG_JOB).get("print-color-mode").first == "monochrome"
    assert sent.group(c.TAG_OPERATION).get("print-color-mode") is None
    assert [g.tag for g in sent.groups] == [c.TAG_OPERATION, c.TAG_JOB]


async def test_not_asked_is_corrected_silently_and_a_printer_without_the_feature_is_left_alone(gw):
    limit(gw, color=False, one_sided=False)
    await send(gw, job(printer="color"), "color")                                       # nothing asked on a colour duplex printer
    sent = gw.cups.jobs()[-1]
    assert gw.cups.job_attr(sent, "print-color-mode") == "monochrome" and gw.cups.job_attr(sent, "sides") == "two-sided-long-edge"
    assert open_notices(gw, "job_adjusted") == []
    await send(gw, job(printer="bw", job_attrs=[kw("sides", "one-sided"), kw("print-color-mode", "color")]), "bw")
    sent = gw.cups.jobs()[-1]                                                           # a black and white, one-sided-only printer
    assert gw.cups.job_attr(sent, "print-color-mode") == "color" and gw.cups.job_attr(sent, "sides") == "one-sided"
    assert open_notices(gw, "job_adjusted") == []
    assert [r[0] for r in log_rows(gw)] == ["accepted", "accepted"]


async def test_refuse_mode_refuses_what_was_asked_and_forces_what_was_not(gw, monkeypatch):
    monkeypatch.setattr(settings, "permission_mode", "refuse")
    limit(gw, color=False)
    out = await send(gw, job(job_attrs=[kw("print-color-mode", "color")]))
    assert out.code == c.STATUS_NOT_POSSIBLE and "colour printing is not allowed" in out.get(c.TAG_OPERATION, "status-message")
    assert gw.cups.jobs() == []
    assert log_rows(gw)[0][0] == "denied" and open_notices(gw, "job_refused")[0].detail_json["why"] == "color"
    assert (await send(gw, job())).code == 0                                            # not asked: printed in black and white
    assert gw.cups.job_attr(gw.cups.jobs()[0], "print-color-mode") == "monochrome"


async def test_unknown_capabilities_when_cups_does_not_answer(gw):
    limit(gw, color=False)
    gw.cups.queues.pop("color")                                                         # the queue does not exist for CUPS
    await send(gw, job(), "color")
    assert gw.cups.job_attr(gw.cups.jobs()[0], "print-color-mode") is None              # nothing forced blindly
    await send(gw, job(job_attrs=[kw("print-color-mode", "color")]), "color")
    assert gw.cups.job_attr(gw.cups.jobs()[1], "print-color-mode") == "monochrome"      # but what was asked is corrected


async def test_the_printer_is_asked_for_its_capabilities_once_a_minute_and_only_when_it_matters(gw):
    await send(gw, job())                                                               # no limits: no question at all
    assert gw.cups.caps_asked == 0
    limit(gw, color=False)
    await send(gw, job()); await send(gw, job())
    assert gw.cups.caps_asked == 1


async def test_a_blocked_printer_still_gives_the_block_reason_and_the_quota_is_not_touched_by_permissions(gw):
    from spoolsteward.policy import control
    with gw.sm() as s:
        control.set_printer_flag(s, "color", "blocked", True)
        s.commit()
    limit(gw, printers_mode="list", printers=["bw"])
    out = await send(gw, job())
    assert out.get(c.TAG_OPERATION, "status-message") == control.BLOCKED_REASON
    assert log_rows(gw)[0][2] == control.BLOCKED_REASON and open_notices(gw, "job_refused") == []
    val = await send(gw, job(c.VALIDATE_JOB))
    assert val.get(c.TAG_OPERATION, "status-message") == control.BLOCKED_REASON


async def test_validate_job_follows_the_same_rules_but_never_fails_on_colour_or_sides_in_adjust_mode(gw, monkeypatch):
    limit(gw, color=False, one_sided=False, printers_mode="list", printers=["color"])
    ok = await send(gw, job(c.VALIDATE_JOB, job_attrs=[kw("print-color-mode", "color"), kw("sides", "one-sided")]))
    assert ok.code == 0 and log_rows(gw) == [] and open_notices(gw, "job_adjusted") == []
    refused = await send(gw, job(c.VALIDATE_JOB, printer="bw"), "bw")
    assert refused.code == c.STATUS_NOT_POSSIBLE and "not allowed for this account" in refused.get(c.TAG_OPERATION, "status-message")
    assert log_rows(gw) == []                                                           # a question is not a job
    monkeypatch.setattr(settings, "permission_mode", "refuse")
    assert (await send(gw, job(c.VALIDATE_JOB, job_attrs=[kw("print-color-mode", "color")]))).code == c.STATUS_NOT_POSSIBLE


async def test_the_printer_list_hides_forbidden_printers_for_a_limited_user_only(gw):
    def listing():
        r = c.make_request(0x4002, 1, requesting_user_name="x")
        r.group(c.TAG_OPERATION).attrs.append(c.Attr("requested-attributes", [c.Val(c.TAG_KEYWORD, "printer-info")]))
        return r
    names = lambda m: [g.get("printer-name").first for g in m.groups if g.tag == c.TAG_PRINTER]  # noqa: E731
    assert names(await send(gw, listing(), "")) == ["color", "bw", "pdf"]
    limit(gw, printers_mode="list", printers=["BW", "pdf"])
    assert names(await send(gw, listing(), "")) == ["bw", "pdf"]
    asked = [m for p, m in gw.cups.seen if m.code == 0x4002][-1]
    assert "printer-name" in [v.value for v in asked.group(c.TAG_OPERATION).get("requested-attributes").values]   # the names are asked for
    with gw.sm() as s:                                                                   # the emergency account sees everything
        make_user(gw.sm, settings.bootstrap_admin_username, password=GOOD, admin=True)
        _, key = keys.create_key(s, s.scalars(select(User).where(User.username == settings.bootstrap_admin_username)).one(), "k", 30)
    assert names(await send(gw, listing(), "", user=settings.bootstrap_admin_username, secret=key)) == ["color", "bw", "pdf"]


async def test_anonymous_requests_are_not_limited(gw):
    limit(gw, printers_mode="list", printers=["bw"], color=False)
    r = c.make_request(c.GET_PRINTER_ATTRIBUTES, 1, printer_uri="ipp://gw.test/printers/color")
    resp = await gw.post("/printers/color", content=c.encode(r), headers={"Content-Type": "application/ipp"})
    assert resp.status_code == 200 and c.decode(resp.content).code == 0


async def test_a_user_rule_overrides_the_group(gw):
    limit(gw, color=False)
    with gw.sm() as s:
        permissions.set_rule(s, SubjectType.user, uid_of(gw.sm, "ann"), color=True)
        s.commit()
    await send(gw, job(job_attrs=[kw("print-color-mode", "color")]))
    assert gw.cups.job_attr(gw.cups.jobs()[0], "print-color-mode") == "color"


# ------------------------------------------------------------------------------------------------ the web print form

PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
       b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R/Size 4>>\n%%EOF\n")


@pytest.fixture
async def web(gw):
    import time
    gw.app.state.queues_cache = (time.monotonic(), [
        {"name": "color", "info": "", "model": "", "device_uri": "ipp://p1/ipp/print", "virtual": False, "color": True, "duplex": True, "media": []},
        {"name": "bw", "info": "", "model": "", "device_uri": "ipp://p2/ipp/print", "virtual": False, "color": False, "duplex": False, "media": []}])
    await portal_login(gw, "ann", GOOD)
    gw.token = await csrf_of_page(gw)
    return gw


async def post_print(cl, queue="color", **extra):
    return await cl.post("/me/print", data={"queue": queue, "copies": "1", "csrf_token": cl.token, **extra},
                         files={"file": ("a.pdf", PDF, "application/pdf")})


async def test_web_form_offers_only_allowed_printers_and_options_and_refuses_the_rest(web):
    limit(web, printers_mode="list", printers=["color"], color=False, one_sided=False)
    page = (await web.get("/me/print")).text
    assert 'value="color"' in page and 'value="bw"' not in page
    assert 'name="color"' not in page and 'name="sides"' not in page                  # no colour choice, no one-sided choice
    r = await post_print(web, "bw")
    assert r.headers["location"] == "/me/print" and "not allowed for this account" in (await web.get("/me/print")).text
    assert [x[0] for x in log_rows(web)] == ["denied"] and open_notices(web, "job_refused")[0].detail_json["why"] == "printer"
    assert web.cups.jobs() == []
    await post_print(web, "color")                                                      # colour and sides forced
    sent = web.cups.jobs()[0]
    assert web.cups.job_attr(sent, "print-color-mode") == "monochrome" and web.cups.job_attr(sent, "sides") == "two-sided-long-edge"
    assert [x[0] for x in log_rows(web)] == ["denied", "accepted"]


async def test_web_form_shows_the_double_sided_choice_only_to_those_who_may_and_adjusts_a_crafted_post(web):
    page = (await web.get("/me/print")).text
    assert 'name="sides"' in page and 'name="color"' in page and 'value="two-sided-short-edge"' in page
    await post_print(web, "color", sides="two-sided-short-edge")
    assert web.cups.job_attr(web.cups.jobs()[0], "sides") == "two-sided-short-edge"
    limit(web, one_sided=False, color=False)
    await post_print(web, "color", sides="one-sided", color="color")                    # posted by hand
    sent = web.cups.jobs()[1]
    assert web.cups.job_attr(sent, "sides") == "two-sided-long-edge" and web.cups.job_attr(sent, "print-color-mode") == "monochrome"
    assert open_notices(web, "job_adjusted")[0].detail_json["changed"] == ["color", "one_sided"]
    r = await web.post("/me/print", data={"queue": "color", "copies": "1", "csrf_token": web.token, "sides": "weird"},
                       files={"file": ("a.pdf", PDF, "application/pdf")})
    assert r.status_code == 400


async def test_web_form_outside_the_hours_shows_the_window_and_refuses(web, monkeypatch):
    limit(web, hours_mode="window", hours_days="01234", hours_start=480, hours_end=1020)
    monkeypatch.setattr(audit, "now_utc", lambda: datetime(2026, 10, 17, 10, 0, tzinfo=UTC))
    monkeypatch.setattr(permissions, "in_window", lambda *a: False)
    assert "You can print Mon–Fri 08:00–17:00" in (await web.get("/me/print")).text
    await post_print(web)
    assert web.cups.jobs() == [] and "printing is allowed only Mon–Fri 08:00–17:00" in (await web.get("/me/print")).text
    assert open_notices(web, "job_refused")[0].detail_json["why"] == "hours"


async def test_the_profile_and_the_key_page_list_only_allowed_printers_and_show_the_permissions(web):
    limit(web, printers_mode="list", printers=["bw"], color=False)
    page = (await web.get("/me")).text
    assert "Your permissions" in page and "not allowed: prints are made in black and white" in page
    avail = page.split("Available printers:")[1].split("</p>")[0]
    assert "bw" in avail and "color" not in avail
    r = await web.post("/me/keys", data={"label": "pc", "csrf_token": web.token})
    assert "/printers/bw" in r.text and "/printers/color" not in r.text


# ------------------------------------------------------------------------------------------------ the admin pages

@pytest.fixture
async def adm(portal):
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    make_user(portal.sm, "ann", password=GOOD, group="staff")
    await portal_login(portal, "boss", GOOD)
    portal.token = await csrf_of_page(portal, "/admin/users")
    portal.app.state.queues_cache = None
    portal.app.state.upstream = httpx.AsyncClient(base_url="http://cups", transport=httpx.MockTransport(FakeCups().handler))
    yield portal
    del portal.app.state.upstream


async def flashes(cl, path="/admin/users"):
    import re
    return re.findall(r'class="flash (?:ok|err)">([^<]*)<', (await cl.get(path)).text)


def group_form(cl, **extra):
    return {"csrf_token": cl.token, "name": "staff", "jobs_limit": "", "pages_limit": "", "period": "weekly", "perm_form": "1", **extra}


def user_form(cl, uid, **extra):
    return {"csrf_token": cl.token, "group_id": str(gid_of(cl.sm, "staff")), "enabled": "1", "period": "weekly", "provider": "local",
            "perm_form": "1", **extra}


async def test_group_permissions_round_trip_through_the_form(adm):
    gid = gid_of(adm.sm, "staff")
    page = (await adm.get(f"/admin/groups/{gid}/edit")).text
    assert '<details class="adv" >' in page and "Advanced permissions" in page          # defaults only: closed
    r = await adm.post(f"/admin/groups/{gid}", data=group_form(adm, perm_printers="list", perm_printer=["Color", "bw"], perm_hours="window",
                                                              perm_day=["0", "1", "2"], perm_start="22:00", perm_end="06:00"))
    assert r.status_code == 303
    with adm.sm() as s:
        row = permissions.get_rule(s, SubjectType.group, gid)
        assert (row.color, row.one_sided) == (False, False)                              # the two boxes were left unchecked
        assert row.printers == ["bw", "color"] and row.hours_days == "012" and (row.hours_start, row.hours_end) == (1320, 360)
        audit_row = s.scalars(select(AdminAudit).where(AdminAudit.action == "group.update")).one()
        assert audit_row.detail_json["permissions"]["printers"] == [None, ["bw", "color"]]
    page = (await adm.get(f"/admin/groups/{gid}/edit")).text
    assert 'value="22:00"' in page and 'value="06:00"' in page and '<details class="adv" open>' in page
    r = await adm.post(f"/admin/groups/{gid}", data=group_form(adm, perm_color="1", perm_one_sided="1", perm_printers="all", perm_hours="any"))
    with adm.sm() as s:
        assert permissions.get_rule(s, SubjectType.group, gid) is None                     # all defaults: nothing stored


async def test_a_post_without_the_permission_block_leaves_the_rules_alone(adm):
    gid = gid_of(adm.sm, "staff")
    limit(adm, color=False)
    await adm.post(f"/admin/groups/{gid}", data={"csrf_token": adm.token, "name": "staff", "period": "weekly"})
    with adm.sm() as s:
        assert permissions.get_rule(s, SubjectType.group, gid).color is False


async def test_bad_permission_values_are_refused_with_a_message_and_nothing_is_saved(adm):
    gid = gid_of(adm.sm, "staff")
    bad = [
        (dict(perm_hours="window", perm_day=["0"], perm_start="09:00", perm_end="09:00"), "must be different"),
        (dict(perm_hours="window", perm_day=["0"], perm_start="9am", perm_end="17:00"), "HH:MM"),
        (dict(perm_hours="window", perm_day=["0"], perm_start="25:00", perm_end="17:00"), "HH:MM"),
        (dict(perm_hours="window", perm_day=[], perm_start="09:00", perm_end="17:00"), "at least one day"),
        (dict(perm_hours="window", perm_day=["7"], perm_start="09:00", perm_end="17:00"), "at least one day"),
        (dict(perm_printers="list", perm_printer=[]), "at least one printer"),
        (dict(perm_printers="list", perm_printer=["../etc"]), "Invalid printer name"),
        (dict(perm_printers="list", perm_printer=["a b"]), "Invalid printer name"),
        (dict(perm_printers="everything"), "Invalid permission value"),
        (dict(perm_hours="sometimes"), "Invalid permission value"),
        (dict(perm_color="maybe"), "Invalid permission value"),
    ]
    for extra, expected in bad:
        r = await adm.post(f"/admin/groups/{gid}", data=group_form(adm, **extra))
        assert r.status_code == 303 and r.headers["location"] == f"/admin/groups/{gid}/edit", extra
        assert expected in (await flashes(adm, f"/admin/groups/{gid}/edit"))[-1], extra
    with adm.sm() as s:
        assert s.query(PermissionRule).count() == 0


async def test_user_permissions_override_the_group_and_the_user_is_told(adm):
    ann = uid_of(adm.sm, "ann")
    limit(adm, color=False)
    page = (await adm.get(f"/admin/users/{ann}/edit")).text
    assert "as the group (not allowed)" in page and "as the group (all)" in page and "as the group (any time)" in page
    r = await adm.post(f"/admin/users/{ann}", data=user_form(adm, ann, perm_color="1", perm_one_sided="0", perm_printers="list",
                                                             perm_printer=["bw"], perm_hours=""))
    assert r.status_code == 303
    p = perms_of(adm.sm, "ann")
    assert p.color and not p.one_sided and p.printers == frozenset({"bw"}) and p.window is None
    [n] = open_notices(adm, "permissions_changed")
    assert n.user_id == ann
    with adm.sm() as s:
        detail = s.scalars(select(AdminAudit).where(AdminAudit.action == "user.update")).one().detail_json
        assert detail["permissions"]["color"] == [None, True] and detail["permissions"]["one_sided"] == [None, False]
    await adm.post(f"/admin/users/{ann}", data=user_form(adm, ann, perm_color="1", perm_one_sided="0", perm_printers="list", perm_printer=["bw"]))
    assert len(open_notices(adm, "permissions_changed")) == 1                           # nothing changed: nobody is told again
    await adm.post(f"/admin/users/{ann}", data=user_form(adm, ann))
    assert perms_of(adm.sm, "ann") == permissions.group_perms(adm.sm(), gid_of(adm.sm, "staff"))   # back to "as the group"
    assert len(open_notices(adm, "permissions_changed")) == 2
    with adm.sm() as s:
        assert permissions.get_rule(s, SubjectType.user, ann) is None


async def test_changing_the_group_does_not_notify_users_and_an_administrator_is_not_told_about_themselves(adm):
    gid = gid_of(adm.sm, "staff")
    await adm.post(f"/admin/groups/{gid}", data=group_form(adm))
    assert open_notices(adm, "permissions_changed") == []
    boss = uid_of(adm.sm, "boss")
    with adm.sm() as s:
        s.get(User, boss).group_id = gid
        s.commit()
    await adm.post(f"/admin/users/{boss}", data=user_form(adm, boss, is_admin="1", perm_color="0"))
    assert not perms_of(adm.sm, "boss").color and open_notices(adm, "permissions_changed") == []


async def test_the_emergency_administrators_page_is_disabled_and_the_server_ignores_what_is_posted(adm):
    make_user(adm.sm, settings.bootstrap_admin_username, password=GOOD, admin=True)
    em = uid_of(adm.sm, settings.bootstrap_admin_username)
    page = (await adm.get(f"/admin/users/{em}/edit")).text
    assert "All permissions, can not be limited." in page and "<fieldset disabled>" in page
    await adm.post(f"/admin/users/{em}", data=user_form(adm, em, is_admin="1", perm_color="0", perm_one_sided="0", perm_printers="list", perm_printer=["x"]))
    with adm.sm() as s:
        assert permissions.get_rule(s, SubjectType.user, em) is None
    assert perms_of(adm.sm, settings.bootstrap_admin_username) == Perms(exempt=True)


async def test_the_notices_say_what_happened_in_plain_words(gw):
    uid = uid_of(gw.sm, "ann")
    notices.notify_user(gw.sm, uid, "permissions_changed", {})
    notices.notify_user(gw.sm, uid, "job_refused", {"printer": "color", "why": "hours", "days": "01234", "start": 480, "end": 1020})
    notices.notify_user(gw.sm, uid, "job_refused", {"printer": "color", "why": "one_sided"})
    notices.notify_user(gw.sm, uid, "job_adjusted", {"printer": "bw", "changed": ["color", "one_sided"]})
    await portal_login(gw, "ann", GOOD)
    page = (await gw.get("/me/notices")).text
    for text in ("changed what you may print", "printing is allowed only Mon–Fri 08:00–17:00", "one-sided printing is not allowed",
                 "was made in black and white", "was made double-sided"):
        assert text in page, text


# ------------------------------------------------------------------------------------------------ former names

async def rename(cl, old, new):
    return await cl.post(f"/admin/printers/{old}/rename", data={"csrf_token": cl.token, "new_name": new})


def former(cl):
    with cl.sm() as s:
        return {r.name: (r.display, r.reason) for r in s.scalars(select(PrinterFormer))}


async def test_renaming_moves_the_name_in_the_lists_and_remembers_the_old_one(adm):
    cl = adm
    with cl.sm() as s:
        permissions.set_rule(s, SubjectType.group, gid_of(cl.sm, "staff"), printers_mode="list", printers=["color", "bw"])
        s.commit()
    r = await rename(cl, "color", "Colour2")
    assert r.status_code == 303
    with cl.sm() as s:
        assert permissions.get_rule(s, SubjectType.group, gid_of(cl.sm, "staff")).printers == ["bw", "colour2"]
        assert s.scalars(select(AdminAudit).where(AdminAudit.action == "printer.rename")).one().detail_json["permission_lists"] == 1
    assert former(cl) == {"color": ("color", "renamed")}
    page = (await cl.get("/admin/printers")).text
    assert 'data-name="color"' in page and 'Names used before (<span class="former-count">1</span>)' in page and "renamed on " in page
    assert 'data-name="Colour2"' not in page                                              # a name in use is not offered


async def test_a_deleted_queue_is_offered_and_taking_its_name_again_removes_it(adm):
    cl = adm
    r = await cl.post("/admin/printers/pdf/delete", data={"csrf_token": cl.token})
    assert r.status_code == 303 and former(cl) == {"pdf": ("pdf", "deleted")}
    assert 'data-name="pdf"' in (await cl.get("/admin/printers")).text
    assert (await rename(cl, "bw", "PDF")).status_code == 303                           # a replacement takes the old name
    assert "bw" in former(cl) and "pdf" not in former(cl)
    assert 'data-name="PDF"' not in (await cl.get("/admin/printers")).text


def test_forgetting_a_queue_keeps_its_name_and_the_list_is_short_and_recent_first(portal):
    with portal.sm() as s:
        for i in range(25):
            discovery.forget_queue(s, f"q{i:02d}", "deleted" if i % 2 else "removed_offline")
        s.commit()
        names = [f.name for f in discovery.former_names(s, ["q24"])]
        assert len(names) == 20 and names[0] == "q23" and "q24" not in names              # current queues are not offered
        with pytest.raises(ValueError):
            discovery.note_former(s, "x", "because")


def test_the_migration_seeds_former_names_from_the_audit(tmp_path):
    from alembic import command
    from sqlalchemy import create_engine, text

    from spoolsteward.db.migrate import _config

    eng = create_engine(f"sqlite:///{tmp_path}/m.db")
    with eng.begin() as conn:
        cfg = _config(eng)
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0014")
        for printer, ts in (("HP_Old", "2026-09-01 10:00:00"), ("hp_old", "2026-09-05 11:00:00"), ("Brother", "2026-08-01 09:00:00"), (None, "2026-08-02 09:00:00")):
            conn.execute(text("insert into job_log(ts, kind, state, printer) values (:ts, 'print', 'completed', :p)"), {"ts": ts, "p": printer})
        conn.execute(text("insert into job_log(ts, kind, state, printer) values ('2026-09-06 10:00:00', 'scan', 'completed', 'scanner')"))   # a scan
        command.upgrade(cfg, "head")
        rows = {r[0]: r for r in conn.execute(text("select name, display, removed_at, reason from printer_former"))}
    assert set(rows) == {"hp_old", "brother"}                                             # one row per name, whatever the case
    assert rows["hp_old"][3] == "deleted" and rows["hp_old"][2].startswith("2026-09-05")


async def test_the_printers_and_the_days_sit_under_the_choice_that_uses_them(adm):
    """The printer and day boxes are inside the part that only shows with "only some" / "some hours": a printer can not be
    ticked while the list is not in use (that is how a saved list used to be ignored)."""
    ann = uid_of(adm.sm, "ann")
    page = (await adm.get(f"/admin/users/{ann}/edit")).text
    printers = page.index('data-show="perm_printers" data-value="list"')
    days = page.index('data-show="perm_hours" data-value="window"')
    assert printers < page.index('name="perm_printer"') < days < page.index('name="perm_day"')
    assert 'class="seg"' in page and 'type="radio" name="perm_printers" value="list"' in page


async def test_renaming_can_tell_every_active_user_in_their_notices(adm):
    cl = adm
    assert 'data-confirm-check-name="notify"' in (await cl.get("/admin/printers")).text           # the tick is in the application's own window
    with cl.sm() as s:
        gone = User(username="gone", provider="local", enabled=False)
        s.add(gone)
        s.commit()
    r = await cl.post("/admin/printers/color/rename", data={"csrf_token": cl.token, "new_name": "Colour3", "notify": "1"})
    assert r.status_code == 303
    with cl.sm() as s:
        rows = list(s.scalars(select(Notice).where(Notice.kind == "printer_renamed")))
        names = {s.get(User, n.user_id).username for n in rows}
        assert "ann" in names and "boss" not in names and "gone" not in names                     # not the one who renamed, not a disabled account
        assert all(n.detail_json == {"old": "color", "new": "Colour3"} and n.key == "Colour3" for n in rows)


async def test_renaming_without_the_tick_tells_nobody(adm):
    r = await rename(adm, "color", "Colour4")
    assert r.status_code == 303
    with adm.sm() as s:
        assert not list(s.scalars(select(Notice).where(Notice.kind == "printer_renamed")))


async def test_maintenance_can_tell_the_users_and_leaving_closes_that_notice(adm):
    cl = adm
    await cl.post("/admin/maintenance", data={"csrf_token": cl.token, "on": "1", "notify": "1"})
    with cl.sm() as s:
        rows = list(s.scalars(select(Notice).where(Notice.kind == "maintenance_on", Notice.dismissed_at.is_(None))))
        assert {s.get(User, n.user_id).username for n in rows} == {"ann"}                           # not the administrator who did it
    await cl.post("/admin/maintenance", data={"csrf_token": cl.token, "on": "0", "notify": "1"})
    with cl.sm() as s:
        assert not list(s.scalars(select(Notice).where(Notice.kind == "maintenance_on", Notice.dismissed_at.is_(None))))
        assert list(s.scalars(select(Notice).where(Notice.kind == "maintenance_off")))


async def test_reaching_the_quota_tells_the_user_on_every_refused_print(gw):
    from spoolsteward.db.models import Metric, Period
    from spoolsteward.policy.rules import set_limit

    with gw.sm() as s:
        set_limit(s, SubjectType.user, uid_of(gw.sm, "ann"), Metric.jobs, 0, Period.weekly)
        s.commit()
    for _ in range(2):
        assert (await send(gw, job())).code == c.STATUS_NOT_POSSIBLE
    [n] = open_notices(gw, "quota_reached")                                             # one notice, counted
    assert n.count == 2 and n.key == "weekly:jobs"
    assert n.detail_json["used"] == 0 and n.detail_json["limit"] == 0 and n.detail_json["printer"] == "color"


async def test_a_name_of_the_past_can_be_forgotten(adm):
    cl = adm
    await cl.post("/admin/printers/pdf/delete", data={"csrf_token": cl.token})
    assert "pdf" in former(cl)
    r = await cl.post("/admin/printers/former/forget", data={"csrf_token": cl.token, "name": "PDF"})
    assert r.status_code == 303 and former(cl) == {}
    assert 'data-name="pdf"' not in (await cl.get("/admin/printers")).text
    with cl.sm() as s:
        assert s.scalars(select(AdminAudit).where(AdminAudit.action == "printer.forget_name")).one()
    r = await cl.post("/admin/printers/former/forget", data={"csrf_token": cl.token, "name": "pdf"})   # already gone: no error
    assert r.status_code == 303


async def test_forgetting_a_name_from_the_page_answers_json_without_a_message(adm):
    cl = adm
    await cl.post("/admin/printers/pdf/delete", data={"csrf_token": cl.token})
    r = await cl.post("/admin/printers/former/forget", data={"csrf_token": cl.token, "name": "pdf"}, headers={"X-Fetch": "1"})
    assert r.status_code == 200 and r.json() == {"ok": True, "found": True} and former(cl) == {}
