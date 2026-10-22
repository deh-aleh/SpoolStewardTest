"""Who may log in where.

  portal : any enabled user. A "local" account is checked against its argon2 hash; any other account is
           delegated to its provider (RADIUS). The provider is decided by the row in the database, never by
           the password: a local account can not be opened by the RADIUS server, whatever it says.
  print  : (IPP) a personal print key. With ipp_auth="keys+radius" a RADIUS user may also use the domain
           password (transition mode); local accounts never can.
"""

import logging

from starlette.concurrency import run_in_threadpool

from spoolsteward.auth import keys
from spoolsteward.auth.passwords import check_password
from spoolsteward.auth.users import find_user, resolve_user
from spoolsteward.config import settings
from spoolsteward.db.models import PrintKey, User

log = logging.getLogger(__name__)
LOCAL = "local"


def normalize(username: str) -> str:
    return username.strip().lower()


def _radius(app):
    """The delegated provider (RADIUS) or None when the deployment is local-only."""
    return getattr(app.state, "auth", None)


def _lookup(app, name: str) -> User | None:
    with app.state.sessionmaker() as db:
        return find_user(db, name)


async def portal_login(app, username: str, password: str) -> User | None:
    name = normalize(username)
    if not name or not password:
        return None
    user = await run_in_threadpool(_lookup, app, name)
    provider = _radius(app)
    if user is None:
        # unknown name: only a delegated provider can vouch for a first-time user
        if provider is None or not settings.auto_create_users or not await provider.authenticate(username, password):
            await check_password(None, password)  # same cost as a real check
            return None
        return await run_in_threadpool(_create_delegated, app, provider.name, name)
    if user.provider == LOCAL:
        ok = await check_password(user.password_hash, password)
        return user if ok and user.enabled else None
    if provider is None or not await provider.authenticate(username, password):
        return None
    return user if user.enabled else None


def _create_delegated(app, provider_name: str, name: str) -> User | None:
    with app.state.sessionmaker() as db:
        user = resolve_user(db, provider_name, name, True, settings.default_group)
        # lost a race against a local account with the same name: the local one is not ours to hand out
        return user if user is not None and user.provider != LOCAL and user.enabled else None


def _check_key(app, name: str, key: str, ip: str | None, for_jobs_status: bool = False) -> tuple[User, PrintKey] | None:
    with app.state.sessionmaker() as db:
        user = find_user(db, name)
        if user is None or not user.enabled:
            return None
        row = keys.verify_key(db, user, key, ip, read_only=for_jobs_status)
        return (user, row) if row is not None else None


async def print_login_with_key(app, username: str, secret: str, ip: str | None = None) -> tuple[User | None, PrintKey | None]:
    """IPP Basic credentials -> (User, the print key that was used or None when the RADIUS password was), or (None, None).
    A single-use key that has just printed is accepted here; the caller lets it do read-only operations only."""
    name = normalize(username)
    if not name or not secret:
        return None, None
    found = await run_in_threadpool(_check_key, app, name, secret, ip, True)
    if found is not None:
        return found
    if secret.startswith(keys.KEY_TAG):
        # It is a print key (expired, revoked, switched off, or a guess), never a domain password. It must not go to RADIUS: the
        # failed attempt would count against the person's domain account, and a few of them can lock it.
        return None, None
    if settings.ipp_auth != "keys+radius":
        return None, None
    provider = _radius(app)
    existing = await run_in_threadpool(_lookup, app, name)
    if provider is None or (existing is not None and existing.provider == LOCAL):
        return None, None
    if not await provider.authenticate(username, secret):
        return None, None
    if existing is not None:
        return (existing if existing.enabled else None), None
    user = await run_in_threadpool(_create_delegated, app, provider.name, name) if settings.auto_create_users else None
    return user, None


async def print_login(app, username: str, secret: str, ip: str | None = None) -> User | None:
    """IPP Basic credentials -> User, or None (a single-use key that has already printed does not count)."""
    user, key = await print_login_with_key(app, username, secret, ip)
    if key is not None and key.used_at is not None:
        return None
    return user
