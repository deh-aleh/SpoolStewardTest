"""Safety net: every state-changing portal route refuses a POST without a CSRF token, so a new route that forgets
`Depends(verify_csrf)` fails here instead of shipping. Also the CSRF and client-address helpers themselves."""

import re
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, HTTPException

import app.main as main
from app.admin.common import check_csrf
from app.netutil import client_ip
from tests.conftest import make_user, portal_login

GOOD = "correct horse battery"

# POST routes that legitimately take no CSRF token:
EXCLUDED = {
    ("auth_router", "/login"),        # the login form: there is no session (hence no token) yet; throttled instead
    ("auth_router", "/setup"),        # first-run setup: protected by the one-time setup code, no session exists yet
    ("escl_router", "/eSCL/{path:path}"),        # eSCL gateway for scanner apps: API clients, not browsers
    ("escl_router", "/eSCL-{sid}/{path:path}"),  # same, per-session scan URL
    ("ipp_router", "/{path:path}"),   # IPP gateway: catch-all POST for printing clients, authenticated by key/Basic auth
}


def portal_post_routes():
    found = []
    for name, router in vars(main).items():
        if isinstance(router, APIRouter):
            for r in router.routes:
                if "POST" in getattr(r, "methods", ()) and (name, r.path) not in EXCLUDED:
                    found.append((name, r.path))
    return sorted(found)


ROUTES = portal_post_routes()


def test_the_enumeration_really_finds_the_portal_routes():
    paths = {p for _, p in ROUTES}
    assert len(ROUTES) >= 55                      # 62 at the time of writing: a collapse means the discovery broke
    assert {"/logout", "/admin/users", "/me/keys", "/me/print", "/admin/backups"} <= paths
    assert not any(n in ("escl_router", "ipp_router") for n, _ in ROUTES)


@pytest.mark.parametrize("name,path", ROUTES, ids=[f"{n}:{p}" for n, p in ROUTES])
async def test_post_without_csrf_token_is_forbidden(portal, name, path):
    make_user(portal.sm, "boss", password=GOOD, admin=True)
    await portal_login(portal, "boss", GOOD)
    url = re.sub(r"\{[^}]+\}", "1", path)
    r = await portal.post(url, data={})
    # 403 (not 404/422): the CSRF check runs before the path id is looked up and before the form is validated
    assert r.status_code == 403, f"{path} answered {r.status_code} to a POST without csrf_token"
    r = await portal.post(url, data={"csrf_token": "wrong"})
    assert r.status_code == 403, f"{path} answered {r.status_code} to a wrong csrf_token"


# ---------------------------------------------------------------- helpers

def fake_request(session=None, client=None):
    return SimpleNamespace(session=session if session is not None else {}, client=client)


def test_check_csrf_accepts_only_the_session_token():
    req = fake_request({"csrf": "s3cret"})
    check_csrf(req, "s3cret")                      # passes silently
    for bad in ("", "wrong", "s3cret "):
        with pytest.raises(HTTPException) as e:
            check_csrf(req, bad)
        assert e.value.status_code == 403


def test_check_csrf_refuses_when_the_session_has_no_token():
    for session in ({}, {"csrf": ""}):
        with pytest.raises(HTTPException) as e:
            check_csrf(fake_request(session), "")
        assert e.value.status_code == 403


def test_client_ip_is_the_direct_peer_or_a_question_mark():
    assert client_ip(fake_request(client=SimpleNamespace(host="10.0.0.7", port=4000))) == "10.0.0.7"
    assert client_ip(fake_request(client=None)) == "?"
