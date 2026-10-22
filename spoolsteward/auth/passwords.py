"""Local account passwords: argon2id, slow on purpose (these are guessable human secrets)."""

import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from starlette.concurrency import run_in_threadpool

from spoolsteward.config import settings
from spoolsteward.i18n import _

MIN_LENGTH = 12
MAX_LENGTH = 256  # bounds the work an attacker can make us do per attempt

# 32 MiB, 3 passes, one lane: above the minimum OWASP gives for argon2id (19 MiB, 2 passes, one lane) and a lot lighter than the
# library's default (64 MiB, four lanes) on a Raspberry Pi, where every check holds its memory for a few tenths of a second.
# The cost is written inside every hash: hashes made with other values still verify, only new ones use these.
_hasher = PasswordHasher(time_cost=3, memory_cost=32 * 1024, parallelism=1)
# verified against when the account does not exist, so "no such user" costs as much as "wrong password"
_DUMMY_HASH = _hasher.hash("not-a-real-password-just-for-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    if len(password) > MAX_LENGTH:
        return False
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerificationError, InvalidHashError):
        return False


_gate: tuple | None = None   # (event loop, how many at once, the semaphore)


def _semaphore() -> asyncio.Semaphore:
    """One semaphore per event loop and per value of SPOOLSTEWARD_PASSWORD_CHECKS_AT_ONCE (made here, not at import: a semaphore
    belongs to the loop that first waits on it)."""
    global _gate
    loop, size = asyncio.get_running_loop(), settings.password_checks_at_once
    if _gate is None or _gate[0] is not loop or _gate[1] != size:
        _gate = (loop, size, asyncio.Semaphore(size))
    return _gate[2]


async def check_password(stored_hash: str | None, password: str) -> bool:
    """`verify_password` for a login: only SPOOLSTEWARD_PASSWORD_CHECKS_AT_ONCE checks run at the same time, the others wait their turn
    here, in the event loop (waiting costs nothing, and the worker threads stay free for everything else). Every check holds some memory,
    so a flood of login attempts slows down instead of using all of it."""
    async with _semaphore():
        return await run_in_threadpool(verify_password, stored_hash, password)


def validate_new_password(password: str, username: str = "") -> str | None:
    """Returns an error message, or None if the password is acceptable. Length beats complexity rules."""
    if len(password) < MIN_LENGTH:
        return _("the password must be at least %(n)s characters long", n=MIN_LENGTH)
    if len(password) > MAX_LENGTH:
        return _("the password must be at most %(n)s characters long", n=MAX_LENGTH)
    if username and password.strip().lower() == username.strip().lower():
        return _("the password must not be the username")
    if len(set(password)) < 5:
        return _("the password is too repetitive")
    return None
