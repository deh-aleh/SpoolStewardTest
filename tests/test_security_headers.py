"""The Content-Security-Policy of the portal pages: on every HTML page, never on the IPP/JSON answers, and the three modes."""

import re

from app.config import settings
from app.secheaders import POLICY
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
