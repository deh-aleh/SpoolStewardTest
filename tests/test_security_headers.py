"""The security headers: the Content-Security-Policy on every HTML page (never on the IPP/JSON answers, and its three modes), and the
headers of every answer: nosniff, HSTS on HTTPS only, and for pages no referrer, no camera, no frame."""

import re

import pytest

from spoolsteward.config import Settings, settings
from spoolsteward.secheaders import POLICY, SecurityHeaders
from tests.conftest import make_user, portal_login

GOOD = "correct horse battery"


async def test_every_portal_page_carries_the_policy(portal):
    make_user(portal.sm, "pia", password=GOOD)
    await portal_login(portal, "pia", GOOD)
    for path in ("/login", "/me", "/me/print", "/me/keys", "/me/settings"):
        r = await portal.get(path)
        assert r.headers.get("content-security-policy") == POLICY, path
        assert "content-security-policy-report-only" not in r.headers


def test_the_policy_forbids_inline_code_and_other_origins():
    directives = dict(d.strip().split(" ", 1) for d in POLICY.split(";"))
    assert directives["default-src"] == "'none'"
    for name in ("script-src", "style-src"):
        assert directives[name] == "'self'"                       # no 'unsafe-inline', no 'unsafe-eval', no other origin
    assert "frame-ancestors" in directives and directives["frame-ancestors"] == "'none'"
    assert "unsafe" not in POLICY


async def test_answers_that_are_not_pages_do_not_get_it(portal):
    r = await portal.get("/healthz")
    assert "content-security-policy" not in r.headers


async def test_report_only_and_off(portal, monkeypatch):
    monkeypatch.setattr(settings, "content_security_policy", "report-only")
    r = await portal.get("/login")
    assert r.headers.get("content-security-policy-report-only") == POLICY and "content-security-policy" not in r.headers
    monkeypatch.setattr(settings, "content_security_policy", "off")
    r = await portal.get("/login")
    assert "content-security-policy" not in r.headers and "content-security-policy-report-only" not in r.headers


async def test_no_page_needs_what_the_policy_forbids(admin):
    """The pages must work under the policy: no inline style, script or handler anywhere in what the portal sends."""
    bad = re.compile(r'<style|\sstyle="|<script(?![^>]*\ssrc=)(?![^>]*application/json)|\son[a-z]+="|javascript:')
    for path in ("/admin/users", "/admin/jobs", "/admin/groups", "/admin/backups", "/admin/requests",
                 "/me", "/me/print", "/me/keys", "/me/settings", "/me/scan", "/admin/report", "/admin/actions"):
        page = (await admin.get(path)).text
        found = bad.search(page)
        assert found is None, f"{path}: {page[max(0, found.start() - 40):found.end() + 60]!r}"


# ---------------------------------------------------------------- the other headers (the middleware on its own)

async def answer(scheme: str, headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    """The headers SecurityHeaders gives to an answer a route made with `headers`, for a request that came in on `scheme`."""
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        sent.append(message)

    await SecurityHeaders(app)({"type": "http", "scheme": scheme}, None, send)
    return {k.decode(): v.decode() for k, v in sent[0]["headers"]}


HTML = [(b"content-type", b"text/html; charset=utf-8")]
JSON = [(b"content-type", b"application/json")]


async def test_hsts_is_off_by_default():
    """It makes a browser refuse to click through a certificate error and forces https on every port of the name: only for those who ask."""
    assert Settings(_env_file=None).hsts_seconds == 0
    assert "strict-transport-security" not in await answer("https", HTML)


async def test_hsts_when_turned_on_is_sent_on_every_https_answer_and_never_over_plain_http(monkeypatch):
    monkeypatch.setattr(settings, "hsts_seconds", 2592000)
    assert (await answer("https", HTML))["strict-transport-security"] == "max-age=2592000"
    assert (await answer("https", JSON))["strict-transport-security"] == "max-age=2592000"      # a host-wide promise: every answer
    assert "strict-transport-security" not in await answer("http", HTML)                          # never over plain HTTP


@pytest.mark.parametrize("seconds, header", [(0, None), (86400, "max-age=86400"), (63072000, "max-age=63072000")])
async def test_hsts_takes_the_seconds_of_the_setting(monkeypatch, seconds, header):
    monkeypatch.setattr(settings, "hsts_seconds", seconds)
    assert (await answer("https", HTML)).get("strict-transport-security") == header


async def test_every_answer_says_nosniff_and_only_pages_get_the_page_headers():
    page, other = await answer("https", HTML), await answer("https", JSON)
    assert page["x-content-type-options"] == other["x-content-type-options"] == "nosniff"
    assert page["referrer-policy"] == "same-origin" and page["x-frame-options"] == "DENY"
    assert page["permissions-policy"] == "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    assert page["content-security-policy"] == POLICY
    for name in ("referrer-policy", "permissions-policy", "x-frame-options", "content-security-policy"):
        assert name not in other, name


async def test_a_header_a_route_already_set_is_not_set_twice():
    got = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [*HTML, (b"x-content-type-options", b"nosniff")]})

    async def send(message):
        got.append(message)

    await SecurityHeaders(app)({"type": "http", "scheme": "https"}, None, send)
    assert [k for k, _v in got[0]["headers"]].count(b"x-content-type-options") == 1


async def test_the_real_portal_sends_them(portal):
    r = await portal.get("/login")
    for name in ("x-content-type-options", "referrer-policy", "permissions-policy", "x-frame-options", "content-security-policy"):
        assert name in r.headers, name
    assert (await portal.get("/healthz")).headers["x-content-type-options"] == "nosniff"
