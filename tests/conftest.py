import os
from pathlib import Path

import httpx
import pytest

# The tests marked requires_cups / requires_radius need the docker test stack (docker-compose.test.yml). Without it they
# are skipped. With SPOOLSTEWARD_TESTS_REQUIRE_DOCKER=1 (set in CI) they fail instead: a stack that did not start, or a
# socket the tests cannot open, must never turn into a green run with the IPP proxy tests quietly skipped.
REQUIRE_DOCKER = os.environ.get("SPOOLSTEWARD_TESTS_REQUIRE_DOCKER") == "1"
_START_STACK = "start it with: docker compose -f docker-compose.test.yml up -d --build --wait"

CUPS_TEST_URL = "http://localhost:6310"
CUPS_SOCK = str(Path(__file__).resolve().parent.parent / "docker" / "run" / "cups.sock")  # the test CUPS (docker-compose.test.yml) exposes it here


def cups_up() -> bool:
    try:
        return httpx.get(CUPS_TEST_URL, timeout=1.0).status_code < 500
    except httpx.HTTPError:
        return False


def cups_sock_up() -> bool:
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=CUPS_SOCK), timeout=1.0) as c:
            return c.get("http://localhost/").status_code < 500
    except httpx.HTTPError:
        return False


def radius_up() -> bool:
    import asyncio

    from spoolsteward.auth.radius import RadiusProvider

    p = RadiusProvider("127.0.0.1", 1812, "testing123", timeout=1, retries=1)
    return asyncio.run(p.authenticate("mario", "mario123"))


def _needs_docker(up: bool, missing: str, fail_fixture: str):
    if up or not REQUIRE_DOCKER:
        return pytest.mark.skipif(not up, reason=missing)
    return pytest.mark.usefixtures(fail_fixture)


# in CI the socket counts too: some tests use it, and one of them skips by itself when it is missing
_CUPS_MISSING = ("docker CUPS not reachable on :6310 or through docker/run/cups.sock" if REQUIRE_DOCKER
                 else "docker CUPS not running on :6310")
_RADIUS_MISSING = "docker FreeRADIUS not running on :1812"


@pytest.fixture
def _docker_cups_missing():
    pytest.fail(f"SPOOLSTEWARD_TESTS_REQUIRE_DOCKER=1 but {_CUPS_MISSING}; {_START_STACK}", pytrace=False)


@pytest.fixture
def _docker_radius_missing():
    pytest.fail(f"SPOOLSTEWARD_TESTS_REQUIRE_DOCKER=1 but {_RADIUS_MISSING}; {_START_STACK}", pytrace=False)


requires_cups = _needs_docker(cups_up() and (not REQUIRE_DOCKER or cups_sock_up()), _CUPS_MISSING, "_docker_cups_missing")
requires_radius = _needs_docker(radius_up(), _RADIUS_MISSING, "_docker_radius_missing")


# ---------------------------------------------------------------- shared portal/gateway fixtures

import re  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


class StubRadius:
    """Stands in for the RADIUS provider: accepts exactly the credentials it was given."""

    name = "radius"

    def __init__(self, users: dict[str, str]):
        self.users = users

    async def authenticate(self, username, password):
        return self.users.get(username) == password


def make_user(sm, username, *, provider="local", password=None, admin=False, group=None, enabled=True,
              must_change=False):
    from spoolsteward.auth.passwords import hash_password
    from spoolsteward.db.models import Group, User

    with sm() as s:
        gid = None
        if group:
            g = s.query(Group).filter_by(name=group).first() or Group(name=group)
            s.add(g)
            s.flush()
            gid = g.id
        u = User(username=username, provider=provider, is_admin=admin, enabled=enabled, group_id=gid,
                 must_change_password=must_change,
                 password_hash=hash_password(password) if password else None,
                 password_changed_at=datetime.now(UTC) if password else None)
        s.add(u)
        s.commit()
        return u.id


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture
async def portal(monkeypatch, tmp_path):
    """A fresh in-memory database behind the real app, over https, with a stub RADIUS (users: rad/radpw)."""
    from spoolsteward.auth.throttle import Throttle
    from spoolsteward.config import settings
    from spoolsteward.db.models import Base
    from spoolsteward.main import app

    monkeypatch.setattr(settings, "audit_archive_dir", str(tmp_path / "archive"))
    monkeypatch.setattr(settings, "bootstrap_admin", True)
    # a file, not ":memory:" + one shared connection: the app uses worker threads and background tasks, and on a single
    # shared connection one thread's rollback silently wipes another thread's uncommitted rows (a rare test flake)
    engine = create_engine(f"sqlite:///{tmp_path / 'portal.db'}", connect_args={"check_same_thread": False, "timeout": 15})
    Base.metadata.create_all(engine)
    sm = sessionmaker(engine, expire_on_commit=False)
    app.state.sessionmaker = sm
    app.state.auth = StubRadius({"rad": "radpw"})
    app.state.throttle = Throttle(5, 300)
    app.state.setup_code = None
    app.state.queues_cache = None
    if hasattr(app.state, "upstream"):
        del app.state.upstream            # tests that need CUPS attach their own
    transport = httpx.ASGITransport(app=app, client=("10.0.0.7", 4000))
    async with httpx.AsyncClient(transport=transport, base_url="https://gw.test") as cl:
        cl.sm, cl.app = sm, app
        yield cl


async def portal_login(cl, username, password):
    return await cl.post("/login", data={"username": username, "password": password})


GOOD = "correct horse battery"


@pytest.fixture
async def admin(portal):
    """The portal with an administrator ('boss') logged in; the system groups exist, as after a real start."""
    from spoolsteward.auth.groups import ensure_system_groups

    make_user(portal.sm, "boss", password=GOOD, admin=True)
    ensure_system_groups(portal.sm)
    await portal_login(portal, "boss", GOOD)
    return portal


def user_row(cl, name):
    from sqlalchemy import select

    from spoolsteward.db.models import User

    with cl.sm() as s:
        return s.scalars(select(User).where(User.username == name)).first()


async def csrf_of_page(cl, path="/me"):
    return csrf_from((await cl.get(path)).text)


@pytest.fixture(autouse=True)
def english_by_default(monkeypatch):
    """The suite runs with English, the product's default language, as the installation language, whatever the
    developer's .env says. Tests about languages and translations set another one themselves (see `italian`)."""
    from spoolsteward.config import settings

    monkeypatch.setattr(settings, "language", "en")


@pytest.fixture
def italian(monkeypatch):
    """Italian as the installation language, for the tests that check a translated page or message."""
    from spoolsteward.config import settings

    monkeypatch.setattr(settings, "language", "it")
