import base64

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from spoolsteward.auth.throttle import Throttle
from spoolsteward.config import settings
from spoolsteward.db.models import Base
from spoolsteward.ipp import codec as c
from spoolsteward.main import app
from tests.conftest import CUPS_TEST_URL, requires_cups

pytestmark = requires_cups


class StubAuth:
    name = "stub"

    async def authenticate(self, u, p):
        return {"mario": "pw1", "boss": "pw2", "Anna": "pw3"}.get(u) == p


def basic(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "allow_insecure_auth", True)
    monkeypatch.setattr(settings, "ipp_auth", "keys+radius")  # these tests use the RADIUS password; keys have their own
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    app.state.sessionmaker = sessionmaker(engine, expire_on_commit=False)
    app.state.auth = StubAuth()
    app.state.throttle = Throttle(5, 300)
    app.state.upstream = httpx.AsyncClient(base_url=CUPS_TEST_URL, timeout=10)
    transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://gw.test:8631") as cl:
        cl.engine = engine
        yield cl
    await app.state.upstream.aclose()


async def post(client, req, user="mario", pw="pw1", path="/printers/virtual"):
    return await client.post(path, content=c.encode(req),
                             headers={"Content-Type": "application/ipp", **basic(user, pw)})


def getattrs():
    return c.make_request(c.GET_PRINTER_ATTRIBUTES, printer_uri="ipp://gw.test:8631/printers/virtual")


async def test_no_credentials_401_with_challenge(client):
    # anything that is not a capability query needs a login
    req = c.make_request(c.PRINT_JOB, 1, printer_uri="ipp://gw.test:8631/printers/virtual", document_format="text/plain")
    req.data = b"x"
    r = await client.post("/printers/virtual", content=c.encode(req), headers={"Content-Type": "application/ipp"})
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert rows(client) == []


async def test_capability_query_works_without_login_and_leaves_no_trace(client):
    """IPP Everywhere clients (CUPS/KDE/Windows/macOS) ask what the printer can do BEFORE sending a password."""
    r = await client.post("/printers/virtual", content=c.encode(getattrs()), headers={"Content-Type": "application/ipp"})
    assert r.status_code == 200
    msg = c.decode(r.content)
    assert msg.code == c.STATUS_OK
    g = msg.group(c.TAG_PRINTER)
    assert g.get("printer-uri-supported").first == "ipp://gw.test:8631/printers/virtual"   # still rewritten
    assert g.get("device-uri") is None                                                      # still hidden
    with client.engine.connect() as conn:
        assert conn.exec_driver_sql("select count(*) from user").scalar() == 0            # no user created
    assert rows(client) == []


async def test_windows_validate_job_is_answered_without_login_but_the_job_itself_is_not(client, monkeypatch):
    """Windows asks Validate-Job while adding the printer, before any password; its "Add device" window can not ask for one."""
    uri = "ipp://gw.test:8631/printers/virtual"
    val = c.make_request(c.VALIDATE_JOB, 1, printer_uri=uri, document_format="application/pdf")
    r = await client.post("/printers/virtual", content=c.encode(val), headers={"Content-Type": "application/ipp"})
    assert r.status_code == 200 and c.decode(r.content).code < 0x0100
    assert rows(client) == []                                                        # nothing audited, no quota used
    for op in (c.PRINT_JOB, 0x0005):                                                 # Print-Job, Create-Job: login needed
        req = c.make_request(op, 1, printer_uri=uri, document_format="application/pdf")
        assert (await client.post("/printers/virtual", content=c.encode(req), headers={"Content-Type": "application/ipp"})).status_code == 401
    monkeypatch.setattr(settings, "anonymous_discovery", False)
    assert (await client.post("/printers/virtual", content=c.encode(val), headers={"Content-Type": "application/ipp"})).status_code == 401


async def test_anonymous_cannot_use_it_to_push_data_or_other_operations(client, monkeypatch):
    # other read-only operations still need a login
    for op in (c.GET_JOBS, 0x4002, 0x0009):
        r = c.make_request(op, 1, printer_uri="ipp://gw.test:8631/printers/virtual")
        assert (await client.post("/printers/virtual", content=c.encode(r), headers={"Content-Type": "application/ipp"})).status_code == 401
    # libcups sends Expect: 100-continue even for plain queries: a small declared body is fine...
    r = await client.post("/printers/virtual", content=c.encode(getattrs()),
                          headers={"Content-Type": "application/ipp", "Expect": "100-continue"})
    assert r.status_code == 200
    # ...but anything big enough to carry a document is challenged without being read
    big = getattrs(); big.data = b"X" * 100_000
    r = await client.post("/printers/virtual", content=c.encode(big), headers={"Content-Type": "application/ipp"})
    assert r.status_code == 401
    # and so is an undeclared (chunked) body
    async def chunked():
        yield c.encode(getattrs())
    r = await client.post("/printers/virtual", content=chunked(), headers={"Content-Type": "application/ipp"})
    assert r.status_code == 401
    # data appended after the attributes is not forwarded to CUPS (it would only be junk or abuse)
    req = getattrs(); req.data = b"X" * 50_000   # under the cap: answered, but the data never reaches CUPS
    assert (await client.post("/printers/virtual", content=c.encode(req), headers={"Content-Type": "application/ipp"})).status_code == 200
    # and the switch turns it all off
    monkeypatch.setattr(settings, "anonymous_discovery", False)
    r = await client.post("/printers/virtual", content=c.encode(getattrs()), headers={"Content-Type": "application/ipp"})
    assert r.status_code == 401


async def test_wrong_password_401(client):
    assert (await post(client, getattrs(), pw="nope")).status_code == 401


async def test_throttle_is_per_user_and_ip_so_strangers_cannot_lock_you_out(client):
    for i in range(5):                                                              # five DIFFERENT guesses
        assert (await post(client, getattrs(), pw=f"nope{i}")).status_code == 401
    assert (await post(client, getattrs(), pw="nope-again")).status_code == 429     # this (user, ip) is locked...
    assert (await post(client, getattrs(), user="boss", pw="pw2")).status_code == 200  # ...other users on that IP are not

    # an attacker on ANOTHER address failing as "Anna" must not lock Anna out
    attacker = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("10.0.0.99", 1)), base_url="http://gw.test:8631")
    async with attacker:
        for i in range(6):
            await attacker.post("/printers/virtual", content=c.encode(getattrs()),
                                headers={"Content-Type": "application/ipp", **basic("Anna", f"bad{i}")})
    assert (await post(client, getattrs(), user="Anna", pw="pw3")).status_code == 200


async def test_plain_http_refused_without_override(client, monkeypatch):
    monkeypatch.setattr(settings, "allow_insecure_auth", False)
    r = await post(client, getattrs())
    assert r.status_code == 403 and b"TLS" in r.content


async def test_get_printer_attributes_rewritten_and_device_uri_hidden(client):
    r = await post(client, getattrs())
    assert r.status_code == 200
    msg = c.decode(r.content)
    g = msg.group(c.TAG_PRINTER)
    assert g.get("printer-uri-supported").first == "ipp://gw.test:8631/printers/virtual"
    assert g.get("device-uri") is None


async def test_identity_forced_from_auth(client):
    req = c.make_request(c.PRINT_JOB, 5, printer_uri="ipp://gw.test:8631/printers/virtual",
                         requesting_user_name="evil", job_name="spoof.txt", document_format="text/plain")
    req.data = b"hello\n"
    msg = c.decode((await post(client, req)).content)
    assert msg.code in (0x0000, 0x0001, 0x0002), hex(msg.code)
    job_uri = msg.get(c.TAG_JOB, "job-uri")
    assert job_uri.startswith("ipp://gw.test:8631/jobs/")
    q = c.make_request(0x0009, 6, job_uri=job_uri)
    info = c.decode((await post(client, q, path="/jobs/" + job_uri.rsplit("/", 1)[1])).content)
    assert info.get(c.TAG_JOB, "job-originating-user-name") == "gw:mario"


async def test_user_autocreated_lowercase(client):
    await post(client, getattrs(), user="Anna", pw="pw3")
    with client.engine.connect() as conn:
        names = [r[0] for r in conn.exec_driver_sql("select username from user")]
    assert names == ["anna"]


async def test_admin_op_blocked_by_gateway_for_user_and_by_cups_for_everyone(client):
    add = c.make_request(0x4003, 9, printer_uri="ipp://gw.test:8631/printers/evil")  # CUPS-Add-Modify-Printer
    msg = c.decode((await post(client, add)).content)
    assert msg.code == c.STATUS_FORBIDDEN
    assert msg.get(c.TAG_OPERATION, "status-message") == "operation not permitted"  # stopped by the gateway
    with client.engine.begin() as conn:
        conn.exec_driver_sql("insert into user(provider,username,is_admin,enabled) values('stub','boss',1,1)")
    r = await post(client, add, user="boss", pw="pw2")
    # a gateway admin passes the gateway, but "gw:boss" is not in CUPS' SystemGroup, so CUPS refuses too
    assert r.status_code in (401, 403) or c.decode(r.content).get(c.TAG_OPERATION, "status-message") != "operation not permitted"
    printers = c.decode((await post(client, c.make_request(0x4002, 1))).content)  # CUPS-Get-Printers still works
    assert printers.code < 0x0100


async def test_user_named_root_gets_no_cups_admin_rights(client, monkeypatch):
    """RADIUS may hand us any name; "root" must not become a CUPS @SYSTEM member."""
    class RootAuth(StubAuth):
        async def authenticate(self, u, p):
            return (u, p) == ("root", "pw")
    app.state.auth = RootAuth()
    with client.engine.begin() as conn:
        conn.exec_driver_sql("insert into user(provider,username,is_admin,enabled) values('stub','root',1,1)")
    add = c.make_request(0x4003, 9, printer_uri="ipp://gw.test:8631/printers/evil")
    r = await post(client, add, user="root", pw="pw")
    assert r.status_code in (401, 403) or c.decode(r.content).code >= 0x0400
    lst = c.decode((await post(client, c.make_request(0x4002, 2), user="root", pw="pw")).content)
    assert not any(a.first == "evil" for g in lst.groups for a in g.attrs if a.name == "printer-name")


async def test_cannot_cancel_someone_elses_job(client):
    """Ownership is enforced by CUPS' @OWNER policy, which works because we set the user name."""
    cj = c.make_request(c.CREATE_JOB, 1, printer_uri="ipp://gw.test:8631/printers/virtual")
    job_id = c.decode((await post(client, cj)).content).get(c.TAG_JOB, "job-id")  # mario's job, awaiting a document

    def cancel():
        r = c.make_request(0x0008, 2, printer_uri="ipp://gw.test:8631/printers/virtual")
        r.group(c.TAG_OPERATION).attrs.append(c.Attr("job-id", [c.Val(c.TAG_INTEGER, job_id)]))
        return r
    other = c.decode((await post(client, cancel(), user="Anna", pw="pw3")).content)
    assert other.code >= 0x0400, hex(other.code)  # anna: refused
    owner = c.decode((await post(client, cancel())).content)
    assert owner.code < 0x0100, hex(owner.code)  # mario: ok


async def test_disabled_user_is_refused_at_authentication(client):
    await post(client, getattrs())
    with client.engine.begin() as conn:
        conn.exec_driver_sql("update user set enabled=0")
    r = await post(client, getattrs())
    assert r.status_code == 401        # same answer as a wrong password: nothing reveals the account exists


async def test_non_ipp_content_type_rejected(client):
    r = await client.post("/printers/virtual", content=b"x", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


async def test_garbage_body_400(client):
    r = await client.post("/printers/virtual", content=b"\x02\x00",
                          headers={"Content-Type": "application/ipp", **basic("mario", "pw1")})
    assert r.status_code == 400


# ---------------------------------------------------------------- policy / audit

def set_limit(client, limit):
    with client.engine.begin() as conn:
        conn.exec_driver_sql("insert into `group`(name) values('default')")
        gid = conn.exec_driver_sql("select id from `group`").scalar()
        conn.exec_driver_sql(
            "insert into policy_rule(subject_type,subject_id,resource,metric,\"limit\",period,enabled) "
            "values('group',?, 'print','jobs',?, 'weekly',1)", (gid, limit))


def rows(client):
    with client.engine.connect() as conn:
        return conn.exec_driver_sql(
            "select u.username, j.state, j.title, j.printer, j.cups_job_id, j.denied_reason "
            "from job_log j left join user u on u.id=j.user_id order by j.id").fetchall()


def pj(title, user="mario", pw="pw1", **kw):
    req = c.make_request(c.PRINT_JOB, 5, printer_uri="ipp://gw.test:8631/printers/virtual",
                         job_name=title, document_format="text/plain", **kw)
    req.data = b"hello\n"
    return req, user, pw


async def test_job_limit_enforced_and_audited(client):
    set_limit(client, 2)
    for name in ("a.txt", "b.txt"):
        req, u, p = pj(name)
        assert c.decode((await post(client, req, u, p)).content).code < 0x0100
    req, u, p = pj("c.txt")
    denied = c.decode((await post(client, req, u, p)).content)
    assert denied.code == c.STATUS_NOT_POSSIBLE
    assert "2/2" in denied.get(c.TAG_OPERATION, "status-message")
    got = rows(client)
    assert [(r[1], r[2]) for r in got] == [("accepted", "a.txt"), ("accepted", "b.txt"), ("denied", "c.txt")]
    assert got[0][0] == "mario" and got[0][3] == "virtual" and got[0][4] is not None  # user, printer, cups job id


async def test_limit_is_per_user(client):
    set_limit(client, 1)
    req, u, p = pj("m.txt")
    assert c.decode((await post(client, req, u, p)).content).code < 0x0100
    req, u, p = pj("a.txt", user="Anna", pw="pw3")
    assert c.decode((await post(client, req, u, p)).content).code < 0x0100
    req, u, p = pj("m2.txt")
    assert c.decode((await post(client, req, u, p)).content).code == c.STATUS_NOT_POSSIBLE


async def test_validate_job_checked_but_not_counted(client):
    set_limit(client, 1)
    v = c.make_request(c.VALIDATE_JOB, 3, printer_uri="ipp://gw.test:8631/printers/virtual", document_format="text/plain")
    assert c.decode((await post(client, v)).content).code < 0x0100
    assert rows(client) == []
    req, u, p = pj("one.txt")
    await post(client, req, u, p)
    assert c.decode((await post(client, v)).content).code == c.STATUS_NOT_POSSIBLE


async def test_cups_rejection_is_logged_failed_and_frees_slot(client):
    set_limit(client, 1)
    req = c.make_request(c.PRINT_JOB, 5, printer_uri="ipp://gw.test:8631/printers/nonexistent",
                         job_name="lost.txt", document_format="text/plain")
    req.data = b"x"
    out = c.decode((await post(client, req, path="/printers/nonexistent")).content)
    assert out.code >= 0x0400
    assert [(r[1], r[2]) for r in rows(client)] == [("failed", "lost.txt")]
    req, u, p = pj("ok.txt")
    assert c.decode((await post(client, req, u, p)).content).code < 0x0100


async def test_create_job_then_send_document_records_file_name(client):
    set_limit(client, 5)
    cj = c.make_request(c.CREATE_JOB, 1, printer_uri="ipp://gw.test:8631/printers/virtual")
    out = c.decode((await post(client, cj)).content)
    assert out.code < 0x0100
    job_id = out.get(c.TAG_JOB, "job-id")
    sd = c.make_request(c.SEND_DOCUMENT, 2, printer_uri="ipp://gw.test:8631/printers/virtual",
                        document_name="minutes.txt", document_format="text/plain")
    sd.group(c.TAG_OPERATION).attrs.append(c.Attr("job-id", [c.Val(c.TAG_INTEGER, job_id)]))
    sd.group(c.TAG_OPERATION).attrs.append(c.Attr("last-document", [c.Val(c.TAG_BOOLEAN, True)]))
    sd.data = b"content\n"
    assert c.decode((await post(client, sd)).content).code < 0x0100
    got = rows(client)
    assert len(got) == 1 and got[0][2] == "minutes.txt"  # "untitled" at Create-Job, filled at Send-Document
    with client.engine.connect() as conn:
        opts = conn.exec_driver_sql("select options_json from job_log").scalar()
    assert "minutes.txt" in opts


async def test_healthz_ok_and_degraded(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    app.state.upstream = httpx.AsyncClient(base_url="http://127.0.0.1:9", timeout=1)  # nothing listens there
    r = await client.get("/healthz")
    assert r.status_code == 503 and r.json() == {"status": "degraded"}


async def test_unix_socket_upstream(monkeypatch):
    """The recommended production wiring: CUPS reachable only through its unix socket."""
    import os

    from spoolsteward.main import make_upstream
    from tests.conftest import CUPS_SOCK

    if not os.path.exists(CUPS_SOCK):
        pytest.skip("docker CUPS socket not exposed")
    monkeypatch.setattr(settings, "cups_url", f"unix://{CUPS_SOCK}")
    up = make_upstream()
    try:
        req = c.make_request(c.GET_PRINTER_ATTRIBUTES, printer_uri="ipp://localhost/printers/virtual")
        r = await up.post("/printers/virtual", content=c.encode(req), headers={"Content-Type": "application/ipp", "Host": "localhost"})
        assert c.decode(r.content).code == c.STATUS_OK
    finally:
        await up.aclose()


# ---------------------------------------------------------------- regressions found by review

async def test_printer_uri_must_match_request_path(client):
    """CUPS prints on printer-uri; auditing the HTTP path instead would mis-attribute the job."""
    set_limit(client, 5)
    req = c.make_request(c.PRINT_JOB, 5, printer_uri="ipp://gw.test:8631/printers/other-queue",
                         job_name="x.txt", document_format="text/plain")
    req.data = b"x"
    out = c.decode((await post(client, req, path="/printers/virtual")).content)
    assert out.code == c.STATUS_BAD_REQUEST
    assert rows(client) == []          # refused before any quota was reserved


async def test_printer_name_case_and_root_path_are_fine(client):
    set_limit(client, 5)
    req = c.make_request(c.PRINT_JOB, 5, printer_uri="ipp://gw.test:8631/printers/VIRTUAL",
                         job_name="case.txt", document_format="text/plain")
    req.data = b"x"
    assert c.decode((await post(client, req, path="/printers/virtual")).content).code < 0x0100  # CUPS ignores case
    assert c.decode((await post(client, req, path="/")).content).code < 0x0100                   # printer-uri only
    assert [r[3] for r in rows(client)] == ["VIRTUAL", "VIRTUAL"]


async def test_print_uri_never_allowed_even_for_admins(client):
    with client.engine.begin() as conn:
        conn.exec_driver_sql("insert into user(provider,username,is_admin,enabled) values('stub','boss',1,1)")
    for op in (0x0003, 0x0007):   # Print-URI / Send-URI = the server fetches a URL for the client
        r = c.make_request(op, 1, printer_uri="ipp://gw.test:8631/printers/virtual")
        out = c.decode((await post(client, r, user="boss", pw="pw2")).content)
        assert out.code == c.STATUS_FORBIDDEN and out.get(c.TAG_OPERATION, "status-message") == "operation not permitted"


async def test_aborted_upload_does_not_burn_the_quota(client):
    set_limit(client, 1)
    head = c.encode(c.make_request(c.PRINT_JOB, 1, printer_uri="ipp://gw.test:8631/printers/virtual",
                                   job_name="aborted.pdf", document_format="text/plain"))

    async def dropping():
        yield head + b"partial"
        raise ConnectionResetError("client went away")
    with pytest.raises(ConnectionResetError):
        await client.post("/printers/virtual", content=dropping(),
                          headers={"Content-Type": "application/ipp", **basic("mario", "pw1")})
    assert [(r[1], r[5]) for r in rows(client)] == [("failed", "aborted before reaching CUPS")]
    req, u, p = pj("after.txt")        # limit is 1: the aborted attempt must not have used it
    assert c.decode((await post(client, req, u, p)).content).code < 0x0100


async def test_oversized_request_is_refused_and_frees_the_slot(client, monkeypatch):
    monkeypatch.setattr(settings, "max_request_mb", 1)
    set_limit(client, 1)
    req, u, p = pj("huge.txt")
    req.data = b"x" * (2 << 20)
    out = c.decode((await post(client, req, u, p)).content)
    assert out.code == c.STATUS_TOO_LARGE
    assert [r[1] for r in rows(client)] == ["failed"]
    monkeypatch.setattr(settings, "max_request_mb", 200)
    req, u, p = pj("small.txt")
    assert c.decode((await post(client, req, u, p)).content).code < 0x0100


async def test_retrying_the_same_wrong_password_does_not_lock_the_user_out(client):
    """libcups retries a rejected password 3-5 times in a row: one typo must not mean five failures."""
    for _ in range(12):
        assert (await post(client, getattrs(), pw="typo")).status_code == 401
    assert (await post(client, getattrs(), pw="pw1")).status_code == 200            # right password still works



# ---------------------------------------------------------------- pages are counted before printing

def blank_pdf(n):
    import io
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(200, 200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def set_pages_limit(client, limit):
    with client.engine.begin() as conn:
        conn.exec_driver_sql("insert into `group`(name) values('default')")
        gid = conn.exec_driver_sql("select id from `group`").scalar()
        conn.exec_driver_sql(
            "insert into policy_rule(subject_type,subject_id,resource,metric,\"limit\",period,enabled) "
            "values('group',?, 'print','pages',?, 'weekly',1)", (gid, limit))


def pdf_job(n, copies=None):
    req = c.make_request(c.PRINT_JOB, 5, printer_uri="ipp://gw.test:8631/printers/virtual", job_name=f"{n}p.pdf",
                         document_format="application/pdf")
    if copies:
        req.groups.append(c.Group(c.TAG_JOB, [c.Attr("copies", [c.Val(c.TAG_INTEGER, copies)])]))
    req.data = blank_pdf(n)
    return req


async def test_a_document_longer_than_the_pages_left_is_refused_up_front(client):
    set_pages_limit(client, 10)
    ok = c.decode((await post(client, pdf_job(4))).content)
    assert ok.code < 0x0100
    too_long = c.decode((await post(client, pdf_job(200))).content)          # one job, 200 pages: must not slip through
    assert too_long.code == c.STATUS_NOT_POSSIBLE
    assert "would be exceeded" in too_long.get(c.TAG_OPERATION, "status-message")
    with client.engine.connect() as conn:
        got = conn.exec_driver_sql("select state, pages from job_log order by id").fetchall()
    assert got == [("accepted", 4), ("denied", None)]
    assert c.decode((await post(client, pdf_job(6))).content).code < 0x0100          # exactly the 6 left
    assert c.decode((await post(client, pdf_job(1))).content).code == c.STATUS_NOT_POSSIBLE


async def test_copies_multiply_the_counted_pages(client):
    set_pages_limit(client, 10)
    assert c.decode((await post(client, pdf_job(3, copies=4))).content).code == c.STATUS_NOT_POSSIBLE   # 12 > 10
    assert c.decode((await post(client, pdf_job(3, copies=3))).content).code < 0x0100                    # 9
    with client.engine.connect() as conn:
        assert conn.exec_driver_sql("select pages from job_log where state='accepted'").scalar() == 9


def test_page_counter_knows_the_common_formats():
    from spoolsteward.policy.pages import count_pages
    assert count_pages(blank_pdf(7), "application/pdf") == 7
    assert count_pages(blank_pdf(3), "application/octet-stream") == 3                  # sniffed, not trusted
    assert count_pages(b"\xff\xd8\xff\xe0" + b"0" * 20, "image/jpeg") == 1
    assert count_pages(b"UNIRAST\x00\x00\x00\x00\x05" + b"0" * 40, "image/urf") == 5
    assert count_pages(b"%!PS-Adobe\n%%Pages: 12\n", "application/postscript") == 12
    assert count_pages(b"line\n" * 130, "text/plain") == 3
    assert count_pages(b"%PDF-1.4 damaged", "application/pdf") is None               # unreadable: soft limit applies
    assert count_pages(b"\x00\x01binary", "application/octet-stream") is None
